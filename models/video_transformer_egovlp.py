from collections import OrderedDict
from functools import partial
import yaml

import torch
from einops import rearrange, repeat
from timm.models.layers import DropPath, to_2tuple, trunc_normal_
from torch import einsum, nn
import torch.nn.functional as F

from models.vision_transformer_dinov2 import vit_base_custom


def state_dict_data_parallel_fix(load_state_dict, curr_state_dict):
    load_keys = list(load_state_dict.keys())
    curr_keys = list(curr_state_dict.keys())

    redo_dp = False
    undo_dp = False
    if not curr_keys[0].startswith('module.') and load_keys[0].startswith('module.'):   # this
        undo_dp = True
    elif curr_keys[0].startswith('module.') and not load_keys[0].startswith('module.'):
        redo_dp = True

    if undo_dp: # this
        from collections import OrderedDict
        new_state_dict = OrderedDict()
        for k, v in load_state_dict.items():
            name = k[7:]  # remove `module.`
            new_state_dict[name] = v
        # load params
    elif redo_dp:
        from collections import OrderedDict
        new_state_dict = OrderedDict()
        for k, v in load_state_dict.items():
            name = 'module.' + k  # remove `module.`
            new_state_dict[name] = v
    else:
        new_state_dict = load_state_dict
    return new_state_dict


DIM_TEXT = 768


class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class VideoPatchEmbed(nn.Module):
    """ Video to Patch Embedding
    """

    def __init__(self, img_size=224, patch_size=16, in_chans=3, embed_dim=768,
                 num_frames=8):
        super().__init__()
        img_size = to_2tuple(img_size)
        patch_size = to_2tuple(patch_size)
        num_patches = (img_size[1] // patch_size[1]) * (img_size[0] // patch_size[0]) * num_frames
        self.img_size = img_size
        self.patch_size = patch_size
        self.num_patches = num_patches
        self.num_frames = num_frames
        self.embed_dim = embed_dim
        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=patch_size)

    def forward(self, x):
        B, F, C, H, W = x.shape
        assert F == self.num_frames, print(F, self.num_frames)
        x = x.reshape(-1, C, H, W)
        x = self.proj(x)
        return x


def attn(q, k, v, ):
    sim = einsum('b i d, b j d -> b i j', q, k)
    attn = sim.softmax(dim=-1)
    out = einsum('b i j, b j d -> b i d', attn, v)

    return out


class VarAttention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0.,
                 initialize='random', dim_text=None, norm_layer=nn.LayerNorm, space_attn=True, ):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        # NOTE scale factor was wrong in my original version, can set manually to be compat with prev weights
        self.scale = qk_scale or head_dim ** -0.5
        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.proj = nn.Linear(dim, dim)
        if initialize == 'zeros':
            self.qkv.weight.data.fill_(0)
            self.qkv.bias.data.fill_(0)
            # fill proj weight with 1 here to improve training dynamics. Otherwise temporal attention inputs
            # are multiplied by 0*0, which is hard for the model to move out of.
            self.proj.weight.data.fill_(1)
            self.proj.bias.data.fill_(0)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj_drop = nn.Dropout(proj_drop)

        self.softmax = nn.Softmax(dim=-1)

        if dim_text is not None and space_attn:
            self.qkv_text_i2t = nn.Linear(dim_text, dim * 2, bias=qkv_bias)
            self.qkv_i2t = nn.Linear(dim, dim, bias=qkv_bias)
            self.attn_drop_i2t = nn.Dropout(attn_drop)
            self.proj_i2t = nn.Linear(dim, dim)
            self.proj_drop_i2t = nn.Dropout(proj_drop)
            self.alpha_i2t = nn.Parameter(torch.Tensor([0]))
            self.norm_i2t_i = norm_layer(dim)

    def forward(self, x, einops_from, einops_to, y=None, y_mask=None, **einops_dims):
        h = self.num_heads
        # project x to q, k, v vaalues
        q, k, v = self.qkv(x).chunk(3, dim=-1)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> (b h) n d', h=h), (q, k, v))

        q = q*self.scale

        # splice out CLS token at index 1
        (cls_q, q_), (cls_k, k_), (cls_v, v_) = map(lambda t: (t[:, 0:1], t[:, 1:]), (q, k, v))

        # let CLS token attend to key / values of all patches across time and space
        cls_out = attn(cls_q, k, v)
        # rearrange across time or space
        q_, k_, v_ = map(lambda t: rearrange(t, f'{einops_from} -> {einops_to}', **einops_dims), (q_, k_, v_))

        # expand cls token keys and values across time or space and concat
        r = q_.shape[0] // cls_k.shape[0]
        cls_k, cls_v = map(lambda t: repeat(t, 'b () d -> (b r) () d', r=r), (cls_k, cls_v))

        k_ = torch.cat((cls_k, k_), dim=1)
        v_ = torch.cat((cls_v, v_), dim=1)

        out = attn(q_, k_, v_,)

        # merge back time or space
        out = rearrange(out, f'{einops_to} -> {einops_from}', **einops_dims)

        # concat back the cls token
        out = torch.cat((cls_out, out), dim=1)

        # merge back the heads
        out = rearrange(out, '(b h) n d -> b n (h d)', h=h)
        ## to out
        x = self.proj(out)
        x = self.proj_drop(x)

        if y is not None:
            B_, N, C = x.shape
            B_text, N_text, C_text = y.shape

            kv_text = (
                self.qkv_text_i2t(y)
                .reshape(B_text, N_text, 2, self.num_heads, C // self.num_heads)
                .permute(2, 0, 3, 1, 4)
            )
            k_text, v_text = kv_text[0], kv_text[1]

            q_i2t = self.qkv_i2t(self.norm_i2t_i(x))
            q_i2t = q_i2t.reshape(B_, N, 1, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
            q_i2t = q_i2t[0]

            # image to text attention
            text_scale = k_text.size(-1) ** -0.5
            q_i2t = q_i2t * text_scale
            attn_i2t = q_i2t @ k_text.transpose(-2, -1)  # B_, nH, N, N_text

            # add image to text bias and text_mask
            if y_mask is not None:
                mask_and_i2t_bias = y_mask.view(B_text, 1, 1, N_text)
                attn_i2t = attn_i2t + mask_and_i2t_bias

            attn_i2t = self.softmax(attn_i2t)
            attn_i2t = self.attn_drop_i2t(attn_i2t)
            y = (attn_i2t @ v_text).transpose(1, 2).reshape(B_, N, C)
            y = self.proj_i2t(y)
            y = self.proj_drop_i2t(y)
            x = x + self.alpha_i2t * y

        return x


class SpaceTimeBlock(nn.Module):
    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm, time_init='zeros',
                 attention_style='frozen-in-time', dim_text=None,):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = VarAttention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop, dim_text=dim_text, 
            norm_layer=norm_layer, space_attn=True,
            )

        self.timeattn = VarAttention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop,
            initialize=time_init, dim_text=dim_text, norm_layer=norm_layer, space_attn=False,
            )

        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)
        self.norm3 = norm_layer(dim)

        self.attention_style = attention_style

    def forward(self, x, einops_from_space, einops_to_space, einops_from_time, einops_to_time,
                time_n, space_f, y=None, y_mask=None):
        # assert y is None

        time_output = self.timeattn(self.norm3(x), einops_from_time, einops_to_time, n=time_n, y=None, y_mask=None)
        time_residual = x + time_output
        space_output = self.attn(self.norm1(time_residual), einops_from_space,
                                    einops_to_space, f=space_f, y=y, y_mask=y_mask)
        if self.attention_style == 'frozen-in-time':
            space_residual = x + self.drop_path(space_output)
        else:
            raise NotImplementedError

        x = space_residual + self.drop_path(self.mlp(self.norm2(space_residual)))

        return x


class SpaceTimeTransformer(nn.Module):
    """ Vision Transformer

    A PyTorch impl of : `Space-Time Transformer` from Frozen-in-time  - by Max Bain.
        https://arxiv.org/abs/2104.00650

    Based off:
     - ViT implementation from the timm library [https://github.com/rwightman/pytorch-image-models/blob/master/timm/models/vision_transformer.py]
    lucidrains timesformer implementation [https://github.com/lucidrains/TimeSformer-pytorch].

    Notable differences:
     - allows for variable length input frames (<= num_frames)
     - allows for variable length input resolution  (<= (img_size, img_size)) [UNTESTED]
     - different attention block mechanism
    """

    def __init__(self, img_size=224, patch_size=16, in_chans=3, num_classes=1000, embed_dim=768, depth=12,
                 num_heads=12, mlp_ratio=4., qkv_bias=True, qk_scale=None, representation_size=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0., hybrid_backbone=None,
                 num_frames=8,actual_num_frames=8, time_init='rand', attention_style='frozen-in-time', norm_layer=nn.LayerNorm, dim_text=None,
                 kwargs=None, ):
        """
        Args:
            img_size (int, tuple): input image size
            patch_size (int, tuple): patch size
            in_chans (int): number of input channels
            num_classes (int): number of classes for classification head
            embed_dim (int): embedding dimension
            depth (int): depth of transformer
            num_heads (int): number of attention heads
            mlp_ratio (int): ratio of mlp hidden dim to embedding dim
            qkv_bias (bool): enable bias for qkv if True
            qk_scale (float): override default qk scale of head_dim ** -0.5 if set
            representation_size (Optional[int]): enable and set representation layer (pre-logits) to this value if set
            drop_rate (float): dropout rate
            attn_drop_rate (float): attention dropout rate
            drop_path_rate (float): stochastic depth rate
            hybrid_backbone (nn.Module): CNN backbone to use in-place of PatchEmbed module
            norm_layer: (nn.Module): normalization layer
            num_frames: (int) maximum number of frames expected as input
            time_init: (str) how to initialise the time attention layer, 'zeros' allows for the timesformer to start off
                        as ViT.
            attention_style: (str) how to attend to space and time.
        """
        super().__init__()
        self.num_classes = num_classes
        self.num_features = self.embed_dim = embed_dim  # num_features for consistency with other models
        self.num_frames = num_frames
        self.actual_num_frames = actual_num_frames
        self.embed_dim = embed_dim

        self.use_relativeCameraPoseLoss = False
        self.relativeCameraPoseLoss_frameType = "all"
        self.use_egovlpV2_patchLevelVisualFeats = False
        if kwargs is not None:
            self.use_relativeCameraPoseLoss = kwargs["use_relativeCameraPoseLoss"] if ("use_relativeCameraPoseLoss" in kwargs) else False
            self.relativeCameraPoseLoss_frameType = kwargs["relativeCameraPoseLoss_frameType"] if ("relativeCameraPoseLoss_frameType" in kwargs) else "all"
            self.use_egovlpV2_patchLevelVisualFeats = kwargs["use_egovlpV2_patchLevelVisualFeats"] if ("use_egovlpV2_patchLevelVisualFeats" in kwargs) else False

        if num_frames != actual_num_frames:
            assert actual_num_frames % num_frames == 0
            if not self.use_egovlpV2_patchLevelVisualFeats:
                raise NotImplementedError

        norm_layer = norm_layer or partial(nn.LayerNorm, eps=1e-6)
        print("######USING ATTENTION STYLE: ", attention_style)
        if hybrid_backbone is not None:
            raise NotImplementedError('hybrid backbone not implemented')
        else:
            self.patch_embed = VideoPatchEmbed(
                img_size=img_size, patch_size=patch_size, in_chans=in_chans, embed_dim=embed_dim, num_frames=num_frames)
        num_patches = self.patch_embed.num_patches
        self.patches_per_frame = num_patches // num_frames

        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        self.pos_embed = nn.Parameter(
            torch.zeros(1, self.patches_per_frame + 1,
                        embed_dim))  # remember to take pos_embed[1:] for tiling over time
        self.temporal_embed = nn.Parameter(torch.zeros(1, num_frames, embed_dim))

        self.pos_drop = nn.Dropout(p=drop_rate)

        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.blocks = nn.ModuleList([
            SpaceTimeBlock(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer, time_init=time_init,
                attention_style=attention_style, dim_text=None if i < 6 else DIM_TEXT,)
            for i in range(depth)])
        self.norm = norm_layer(embed_dim)

        # Representation layer
        if representation_size:
            self.num_features = representation_size
            self.pre_logits = nn.Sequential(OrderedDict([
                ('fc', nn.Linear(embed_dim, representation_size)),
                ('act', nn.Tanh())
            ]))
        else:
            self.pre_logits = nn.Identity()

        # Classifier head
        self.head = nn.Linear(self.num_features, num_classes) if num_classes > 0 else nn.Identity()

        trunc_normal_(self.pos_embed, std=.02)
        trunc_normal_(self.cls_token, std=.02)

        # if num_frames > 1, then we perform ViT inflation and initialise time attention to zero so not necessary.
        if num_frames == 1:
            self.apply(self._init_weights)

        ## einops transformations
        self.einops_from_space = 'b (f n) d'
        self.einops_to_space = '(b f) n d'
        self.einops_from_time = 'b (f n) d'
        self.einops_to_time = '(b n) f d'


    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed', 'cls_token'}

    def get_classifier(self):
        return self.head

    def reset_classifier(self, num_classes, global_pool=''):
        self.num_classes = num_classes
        self.head = nn.Linear(self.embed_dim, num_classes) if num_classes > 0 else nn.Identity()

    def forward_features(self, x):
        b, curr_frames, channels, frame_h, frame_w = x.shape
        if self.actual_num_frames != self.num_frames:
            x = x.reshape((b * (self.actual_num_frames // self.num_frames),
                          self.num_frames, 
                          x.shape[2], 
                          x.shape[3], 
                          x.shape[4]))
            b, curr_frames = x.shape[0], x.shape[1]

        x = self.patch_embed(x)

        x = x.flatten(2).transpose(2, 1)
        x = x.reshape(b, -1, self.patch_embed.embed_dim)

        BF = x.shape[0]
        cls_tokens = self.cls_token.expand(BF, -1, -1)  # stole cls_tokens impl from Phil Wang, thanks
        x = torch.cat((cls_tokens, x), dim=1)
        # positional embed needs to be tiled for each frame (this does [1,2,3] --> [1,2,3,1,2,3]...)
        cls_embed = self.pos_embed[:, 0, :].unsqueeze(1)
        tile_pos_embed = self.pos_embed[:, 1:, :].repeat(1, self.num_frames, 1)
        # temporal embed needs to be repeated within each frame (this does [1,2,3] --> [1,1,1,2,2,2,3,3,3]...)
        tile_temporal_embed = self.temporal_embed.repeat_interleave(self.patches_per_frame, 1)
        total_pos_embed = tile_pos_embed + tile_temporal_embed
        total_pos_embed = torch.cat([cls_embed, total_pos_embed], dim=1)

        curr_patches = x.shape[1]
        x = x + total_pos_embed[:, :curr_patches]
        x = self.pos_drop(x)
        n = self.patches_per_frame
        f = curr_frames

        for i, blk in enumerate(self.blocks):
            check = False
            if check:
            # if config_yaml['use_checkpoint']:

                def create_custom_forward(module):
                    def custom_forward(*inputs):
                        return module(*inputs, time_n=n, space_f=f)

                    return custom_forward

                x = torch.utils.checkpoint.checkpoint(create_custom_forward(blk), x, self.einops_from_space, self.einops_to_space, self.einops_from_time,
                    self.einops_to_time)
            else:
                x = blk(x, self.einops_from_space, self.einops_to_space, self.einops_from_time,
                    self.einops_to_time, time_n=n, space_f=f)

        if self.use_relativeCameraPoseLoss:
            y = self.norm(x)[:, 1:]
            if self.actual_num_frames != self.num_frames:
                y = y.reshape((int(x.shape[0] * (self.num_frames / self.actual_num_frames)), 
                               (self.actual_num_frames // self.num_frames) * y.shape[1],
                               y.shape[2]))
            z = y.reshape((y.shape[0],
                            self.actual_num_frames if (self.num_frames != self.actual_num_frames) else self.num_frames,
                            int(self.patches_per_frame ** 0.5), 
                            int(self.patches_per_frame ** 0.5), 
                            y.shape[2]))

        if self.use_egovlpV2_patchLevelVisualFeats:
            if self.use_relativeCameraPoseLoss:
                x = y
            else:
                x = self.norm(x)[:, 1:]
                if self.actual_num_frames != self.num_frames:
                    x = x.reshape((int(x.shape[0] * (self.num_frames / self.actual_num_frames)), 
                                   (self.actual_num_frames // self.num_frames) * x.shape[1],
                                   x.shape[2]))

            x = x.reshape((x.shape[0],
                            self.actual_num_frames if (self.num_frames != self.actual_num_frames) else self.num_frames,
                            int(self.patches_per_frame ** 0.5), 
                            int(self.patches_per_frame ** 0.5), 
                            x.shape[2]))
        else:
            x = self.norm(x)[:, 0]
            x = self.pre_logits(x)

        if self.use_relativeCameraPoseLoss:
            return x, z
        else:
            return x

    def forward(self, x):
        if self.use_relativeCameraPoseLoss:
            x, y = self.forward_features(x)
            return x, y
        else:
            x = self.forward_features(x)
            return x


# @MODEL_REGISTRY.register()
class EgoVLPv2(nn.Module):
    def __init__(self, ckpt_path=None, num_frames=8,
                 kwargs=None,):
        super(EgoVLPv2, self).__init__()
        self.num_frames = num_frames # 4, 16
        self.load_temporal_fix='bilinear'

        self.num_views = 5
        self.use_relativeCameraPoseLoss = False
        self.useRelu_relativeCameraPoseLoss = False
        self.relativeCameraPoseLoss_poseEncoder_dropout = 0.
        self.relativeCameraPoseLoss_coordsAsClasses = False
        self.relativeCameraPoseLoss_coordsClassSize = 10
        self.relativeCameraPoseLoss_rotationOnly = False
        self.relativeCameraPoseLoss_rotationInAngles = False
        self.relativeCameraPoseLoss_rotationInQuarts = False
        self.relativeCameraPoseLoss_rotationAsClasses = False
        self.relativeCameraPoseLoss_rotationClassSize = 10
        self.relativeCameraPoseLoss_frameType = "all"
        self.relativeCameraPoseLoss_convOutDims = 64
        self.relativeCameraPoseLoss_refType = "first_view"
        self.relativeCameraPoseLoss_stopGradientRefPose = False
        self.use_egovlpV2_patchLevelVisualFeats = False
        self.egovlpV2_patchLevelVisualFeats_convOutDims = 192
        self.egovlpV2_depth = 12
        self.egovlpV2_feedFourFrames = False
        self.egovlpV2_encodeWdinoV2 = False
        self.videoEncoder_dropout = 0.
        if kwargs is not None:
            self.num_views = len(kwargs["all_views"]) if ("all_views" in kwargs) else 5
            self.use_relativeCameraPoseLoss = kwargs["use_relativeCameraPoseLoss"] if ("use_relativeCameraPoseLoss" in kwargs) else False
            self.useRelu_relativeCameraPoseLoss = kwargs["useRelu_relativeCameraPoseLoss"] if ("useRelu_relativeCameraPoseLoss" in kwargs) else False
            self.relativeCameraPoseLoss_poseEncoder_dropout = kwargs["relativeCameraPoseLoss_poseEncoder_dropout"] if ("relativeCameraPoseLoss_poseEncoder_dropout" in kwargs) else 0.
            self.relativeCameraPoseLoss_arc = kwargs["relativeCameraPoseLoss_arc"] if ("relativeCameraPoseLoss_arc" in kwargs) else "cls"
            self.relativeCameraPoseLoss_rotationOnly = kwargs["relativeCameraPoseLoss_rotationOnly"] if ("relativeCameraPoseLoss_rotationOnly" in kwargs) else False
            self.relativeCameraPoseLoss_rotationInAngles = kwargs["relativeCameraPoseLoss_rotationInAngles"] if ("relativeCameraPoseLoss_rotationInAngles" in kwargs) else False
            self.relativeCameraPoseLoss_rotationInQuarts = kwargs["relativeCameraPoseLoss_rotationInQuarts"] if ("relativeCameraPoseLoss_rotationInQuarts" in kwargs) else False
            self.relativeCameraPoseLoss_rotationAsClasses = kwargs["relativeCameraPoseLoss_rotationAsClasses"] if ("relativeCameraPoseLoss_rotationAsClasses" in kwargs) else False
            self.relativeCameraPoseLoss_rotationClassSize = kwargs["relativeCameraPoseLoss_rotationClassSize"] if ("relativeCameraPoseLoss_rotationClassSize" in kwargs) else 10
            self.relativeCameraPoseLoss_coordsAsClasses = kwargs["relativeCameraPoseLoss_coordsAsClasses"] if ("relativeCameraPoseLoss_coordsAsClasses" in kwargs) else False
            self.relativeCameraPoseLoss_coordsClassSize = kwargs["relativeCameraPoseLoss_coordsClassSize"] if ("relativeCameraPoseLoss_coordsClassSize" in kwargs) else 10
            self.relativeCameraPoseLoss_frameType = kwargs["relativeCameraPoseLoss_frameType"] if ("relativeCameraPoseLoss_frameType" in kwargs) else "all"
            self.relativeCameraPoseLoss_convOutDims = kwargs["relativeCameraPoseLoss_convOutDims"] if ("relativeCameraPoseLoss_convOutDims" in kwargs) else 64
            self.relativeCameraPoseLoss_refType = kwargs["relativeCameraPoseLoss_refType"] if ("relativeCameraPoseLoss_refType" in kwargs) else "first_view"
            self.relativeCameraPoseLoss_stopGradientRefPose = kwargs["relativeCameraPoseLoss_stopGradientRefPose"] if ("relativeCameraPoseLoss_stopGradientRefPose" in kwargs) else False
            self.use_egovlpV2_patchLevelVisualFeats = kwargs["use_egovlpV2_patchLevelVisualFeats"] if ("use_egovlpV2_patchLevelVisualFeats" in kwargs) else False
            self.egovlpV2_patchLevelVisualFeats_convOutDims = kwargs["egovlpV2_patchLevelVisualFeats_convOutDims"] if ("egovlpV2_patchLevelVisualFeats_convOutDims" in kwargs) else 192
            self.egovlpV2_depth = kwargs["egovlpV2_depth"] if ("egovlpV2_depth" in kwargs) else 12
            self.egovlpV2_feedFourFrames = kwargs["egovlpV2_feedFourFrames"] if ("egovlpV2_feedFourFrames" in kwargs) else False
            self.egovlpV2_encodeWdinoV2 = kwargs["egovlpV2_encodeWdinoV2"] if ("egovlpV2_encodeWdinoV2" in kwargs) else False
            self.videoEncoder_dropout = kwargs["videoEncoder_dropout"] if ("videoEncoder_dropout" in kwargs) else 0.

        if self.egovlpV2_encodeWdinoV2:
            assert (kwargs["recog_arc"] == "egovlp_v2") and kwargs["unfreeze_videoEncoder"]

        if self.egovlpV2_encodeWdinoV2:
            if self.videoEncoder_dropout != 0.:
                print("implement custom dropout in dinoV2")
                raise NotImplementedError
            self.model = vit_base_custom(kwargs)
        else:
            self.model = SpaceTimeTransformer(num_frames=4 if self.egovlpV2_feedFourFrames else self.num_frames,
                                              actual_num_frames=self.num_frames,
                                              time_init='zeros',
                                              attention_style='frozen-in-time',
                                              kwargs=kwargs,
                                              depth=self.egovlpV2_depth,
                                              drop_rate=self.videoEncoder_dropout, 
                                              attn_drop_rate=self.videoEncoder_dropout, 
                                              drop_path_rate=self.videoEncoder_dropout,)

            self.model.head = nn.Identity()
            self.model.pre_logits = nn.Identity()
            self.model.fc = nn.Identity()

        if ckpt_path is not None:
            self.load_ckpt(ckpt_path,)

        if self.use_relativeCameraPoseLoss:
            num_pose_outputs = num_poseTranslationOutputs = num_poseRotationOutputs = 0
            if not self.relativeCameraPoseLoss_rotationOnly:
                if self.relativeCameraPoseLoss_coordsAsClasses:
                    assert self.relativeCameraPoseLoss_coordsClassSize > 0
                    assert 360 % self.relativeCameraPoseLoss_coordsClassSize == 0
                    assert 180 % self.relativeCameraPoseLoss_coordsClassSize == 0
                    coorsAlpha_numClasses = int(360 // self.relativeCameraPoseLoss_coordsClassSize) + 1
                    coorsBeta_numClasses = int(180 // self.relativeCameraPoseLoss_coordsClassSize) + 1
                    num_poseTranslationOutputs = coorsAlpha_numClasses + coorsBeta_numClasses
                else:
                    num_poseTranslationOutputs = 3
                num_pose_outputs += num_poseTranslationOutputs

            if self.relativeCameraPoseLoss_rotationInAngles:
                if self.relativeCameraPoseLoss_rotationAsClasses:
                    assert self.relativeCameraPoseLoss_rotationClassSize > 0
                    assert 360 % self.relativeCameraPoseLoss_rotationClassSize == 0
                    assert 180 % self.relativeCameraPoseLoss_rotationClassSize == 0
                    rotsAngX_numClasses = rotsAngZ_numClasses = (int(360 // self.relativeCameraPoseLoss_rotationClassSize) + 1)
                    rotsAngY_numClasses = int((180 // self.relativeCameraPoseLoss_rotationClassSize) + 1)
                    num_poseRotationOutputs = rotsAngX_numClasses + rotsAngY_numClasses + rotsAngZ_numClasses
                else:
                    num_poseRotationOutputs = 3
            elif self.relativeCameraPoseLoss_rotationInQuarts:
                num_poseRotationOutputs = 4
            else:
                num_poseRotationOutputs = 9
            num_pose_outputs += num_poseRotationOutputs

            self.shared_conv = nn.Sequential(nn.Conv2d(self.model.num_features,  
                                                192,
                                                kernel_size=1,
                                                stride=1,
                                                padding=0,
                                                bias=False),
                                )

            self.pose_conv = nn.Sequential()
            self.pose_conv.append(nn.BatchNorm2d(192 * 2))
            self.pose_conv.append(nn.ReLU(inplace=True) if self.useRelu_relativeCameraPoseLoss else nn.ELU(inplace=True))
            if self.relativeCameraPoseLoss_poseEncoder_dropout > 0:
                self.pose_conv.append(nn.Dropout(self.relativeCameraPoseLoss_poseEncoder_dropout, inplace=False))
            self.pose_conv.append(nn.Conv2d(192 * 2,
                                            48,
                                            kernel_size=4,
                                            stride=2,
                                            padding=1,
                                            bias=False))

            self.pose_lin = nn.Sequential()
            self.pose_lin.append(nn.BatchNorm1d(2352))
            self.pose_lin.append(nn.ReLU(inplace=True) if self.useRelu_relativeCameraPoseLoss else nn.ELU(inplace=True))
            if self.relativeCameraPoseLoss_poseEncoder_dropout > 0:
                self.pose_lin.append(nn.Dropout(self.relativeCameraPoseLoss_poseEncoder_dropout, inplace=False))
            self.pose_lin.append(nn.Linear(2352, num_pose_outputs, bias=False))

            self.pred_conv = nn.Sequential()
            self.pred_conv.append(nn.BatchNorm2d(192))
            self.pred_conv.append(nn.ReLU(inplace=True),)
            if self.videoEncoder_dropout > 0:
                self.pred_conv.append(nn.Dropout(self.videoEncoder_dropout, inplace=False))
            self.pred_conv.append(nn.Conv2d(192,
                                            96,
                                            kernel_size=4,
                                            stride=2,
                                            padding=1,
                                            bias=False))
            self.pred_conv.append(nn.BatchNorm2d(96))
            self.pred_conv.append(nn.ReLU(inplace=True))
            if self.videoEncoder_dropout > 0:
                self.pred_conv.append(nn.Dropout(self.videoEncoder_dropout, inplace=False))
            self.pred_conv.append(nn.Conv2d(96,
                                            24,
                                            kernel_size=4,
                                            stride=2,
                                            padding=0,
                                            bias=False))

        else:
            if self.use_egovlpV2_patchLevelVisualFeats:
                self.conv_egovlpV2_patchLevelVisualFeats = nn.Sequential()
                self.conv_egovlpV2_patchLevelVisualFeats.append(nn.Conv2d(self.model.num_features,  
                                                                                    192,
                                                                                    kernel_size=1,
                                                                                    stride=1,
                                                                                    padding=0,
                                                                                    bias=False))
                self.conv_egovlpV2_patchLevelVisualFeats.append(nn.BatchNorm2d(192))
                self.conv_egovlpV2_patchLevelVisualFeats.append(nn.ReLU(inplace=True))
                if self.videoEncoder_dropout > 0:
                    self.conv_egovlpV2_patchLevelVisualFeats.append(nn.Dropout(self.videoEncoder_dropout, inplace=False))
                self.conv_egovlpV2_patchLevelVisualFeats.append(nn.Conv2d(192,  
                                                                            96,
                                                                            kernel_size=4,
                                                                            stride=2,
                                                                            padding=1,
                                                                            bias=False))
                self.conv_egovlpV2_patchLevelVisualFeats.append(nn.BatchNorm2d(96))
                self.conv_egovlpV2_patchLevelVisualFeats.append(nn.ReLU(inplace=True))
                if self.videoEncoder_dropout > 0:
                    self.conv_egovlpV2_patchLevelVisualFeats.append(nn.Dropout(self.videoEncoder_dropout, inplace=False))
                self.conv_egovlpV2_patchLevelVisualFeats.append(nn.Conv2d(96,  
                                                                            24,
                                                                            kernel_size=4,
                                                                            stride=2,
                                                                            padding=0,
                                                                            bias=False)) 

            else:
                if self.egovlpV2_encodeWdinoV2:
                    self.egovlpV2_encodeWdinoV2_x_norm_clstoken_agg = nn.Linear(self.model.num_features * self.num_frames, self.model.num_features)


    def _inflate_positional_embeds(self, new_state_dict):
        # allow loading of timesformer with fewer num_frames
        curr_keys = list(self.state_dict().keys())
        if 'model.temporal_embed' in new_state_dict and 'model.temporal_embed' in curr_keys:
            load_temporal_embed = new_state_dict['model.temporal_embed']
            load_num_frames = load_temporal_embed.shape[1]
            curr_num_frames = 4 if self.egovlpV2_feedFourFrames else self.num_frames
            embed_dim = load_temporal_embed.shape[2]

            if load_num_frames != curr_num_frames:
                if load_num_frames > curr_num_frames:
                    print(f'### loaded  model has MORE frames than current...'
                          f'### loading weights, filling in the extras via {self.load_temporal_fix}')
                    new_temporal_embed = load_temporal_embed[:, :curr_num_frames, :]
                else:
                    print(f'### loaded  model has FEWER frames than current...'
                          f'### loading weights, filling in the extras via {self.load_temporal_fix}')
                    if self.load_temporal_fix == 'zeros':
                        new_temporal_embed = torch.zeros([load_temporal_embed.shape[0], curr_num_frames, embed_dim])
                        new_temporal_embed[:, :load_num_frames] = load_temporal_embed
                    elif self.load_temporal_fix in ['interp', 'bilinear']:
                        # interpolate
                        # unsqueeze so pytorch thinks its an image
                        mode = 'nearest'
                        if self.load_temporal_fix == 'bilinear':
                            mode = 'bilinear'
                        load_temporal_embed = load_temporal_embed.unsqueeze(0)
                        new_temporal_embed = F.interpolate(load_temporal_embed,
                                                           (curr_num_frames, embed_dim), mode=mode, align_corners=True).squeeze(0)
                    else:
                        raise NotImplementedError
                new_state_dict['model.temporal_embed'] = new_temporal_embed

    def load_ckpt(self, ckpt_path):
        checkpoint = torch.load(ckpt_path, map_location='cpu')
        if self.egovlpV2_encodeWdinoV2:
            state_dict = checkpoint
            missing_keys, unexpected_keys = self.model.load_state_dict(state_dict, strict=False)
        else:
            state_dict = checkpoint['state_dict']
            new_state_dict = {}
            for key, value in state_dict.items():
                name = key[7:].replace("video_model.", "model.")
                new_state_dict[name] = value
            self._inflate_positional_embeds(new_state_dict)

            missing_keys, unexpected_keys = self.load_state_dict(new_state_dict, strict=False)
        print(f"Loading pretrained model from {ckpt_path}, missing keys are {missing_keys}, unexpected keys are {unexpected_keys}")

    def forward(self, x):
        if self.use_relativeCameraPoseLoss:
            video_embeddings, video_embeddings_finegrained = self.model(x)

            if self.use_egovlpV2_patchLevelVisualFeats:
                video_embeddings = video_embeddings.permute((0, 1, 4, 2, 3))
                B, num_frames = video_embeddings.shape[0], video_embeddings.shape[1]
                video_embeddings = video_embeddings.reshape((B * num_frames,
                                                             video_embeddings.shape[2],
                                                             video_embeddings.shape[3],
                                                             video_embeddings.shape[4],))

                video_embeddings = self.conv_egovlpV2_patchLevelVisualFeats(video_embeddings).squeeze(-1).squeeze(-1)
                video_embeddings = video_embeddings.reshape((B, num_frames, video_embeddings.shape[1])).reshape((B, -1))
                video_embeddings = self.linear_egovlpV2_patchLevelVisualFeats(video_embeddings)
            else:
                if self.egovlpV2_encodeWdinoV2:
                    video_embeddings = self.egovlpV2_encodeWdinoV2_x_norm_clstoken_agg(video_embeddings)

            video_embeddings_finegrained = video_embeddings_finegrained.permute((0, 1, 4, 2, 3))
            if self.relativeCameraPoseLoss_frameType == "center":
                raise ValueError
                video_embeddings_finegrained = video_embeddings_finegrained[:,
                                                                           (video_embeddings_finegrained.shape[1] // 2):\
                                                                            (video_embeddings_finegrained.shape[1] // 2) + 1,
                                                                           ] 
            B, num_frames = video_embeddings_finegrained.shape[0], video_embeddings_finegrained.shape[1]

            video_embeddings_finegrained = video_embeddings_finegrained.reshape((B * num_frames, 
                                                                                    video_embeddings_finegrained.shape[2],
                                                                                    video_embeddings_finegrained.shape[3],
                                                                                    video_embeddings_finegrained.shape[4],))
            video_embeddings_shared = self.shared_conv(video_embeddings_finegrained)
            video_embeddings = self.pred_conv(video_embeddings_shared)
            video_embeddings = video_embeddings.reshape((B, -1))

            if self.relativeCameraPoseLoss_refType in ["first_view", "all_views"]:
                video_embeddings_cameraPose = video_embeddings_shared.reshape((-1,
                                                                                    self.num_views, 
                                                                                    num_frames,
                                                                                    video_embeddings_shared.shape[1],
                                                                                    video_embeddings_shared.shape[2],
                                                                                    video_embeddings_shared.shape[3]))
                B_actl = video_embeddings_cameraPose.shape[0]
                video_embeddings_cameraPose = video_embeddings_cameraPose.permute((0, 2, 1, 3, 4, 5))
                if self.relativeCameraPoseLoss_refType == "first_view":
                    video_embeddings_cameraPose_ref = torch.cat([video_embeddings_cameraPose[:, :, :1]] * self.num_views, dim=2)
                elif self.relativeCameraPoseLoss_refType == "all_views":
                    video_embeddings_cameraPose_ref = []
                    for vw_idx in range(self.num_views):
                        video_embeddings_cameraPose_ref += ([video_embeddings_cameraPose[:, :, vw_idx: vw_idx + 1]] * self.num_views)
                    video_embeddings_cameraPose_ref = torch.cat(video_embeddings_cameraPose_ref, dim=2)
                    video_embeddings_cameraPose = torch.cat([video_embeddings_cameraPose] * self.num_views, dim=2)

                if self.relativeCameraPoseLoss_stopGradientRefPose:
                    video_embeddings_cameraPose_ref = video_embeddings_cameraPose_ref.detach()
                video_embeddings_cameraPose = torch.cat([video_embeddings_cameraPose_ref, video_embeddings_cameraPose], dim=3)
                video_embeddings_cameraPose = video_embeddings_cameraPose.permute((0, 2, 1, 3, 4, 5))
                dm1, dm2, dm3 = video_embeddings_cameraPose.shape[0], video_embeddings_cameraPose.shape[1], video_embeddings_cameraPose.shape[2]
                video_embeddings_cameraPose = video_embeddings_cameraPose.reshape((-1, 
                                                                                    video_embeddings_cameraPose.shape[3], 
                                                                                    video_embeddings_cameraPose.shape[4],
                                                                                    video_embeddings_cameraPose.shape[5]))
                video_embeddings_cameraPose = self.pose_conv(video_embeddings_cameraPose)

                video_embeddings_cameraPose = video_embeddings_cameraPose.reshape((video_embeddings_cameraPose.shape[0], -1))

                video_embeddings_cameraPose = video_embeddings_cameraPose.reshape((dm1, dm2, dm3, video_embeddings_cameraPose.shape[-1]))

                if self.relativeCameraPoseLoss_refType == "first_view":
                    video_embeddings_cameraPose = video_embeddings_cameraPose.reshape((B_actl * self.num_views * num_frames, video_embeddings_cameraPose.shape[3]))
                    video_embeddings_cameraPose = self.pose_lin(video_embeddings_cameraPose)
                    video_embeddings_cameraPose = video_embeddings_cameraPose.reshape((B_actl * self.num_views, num_frames, video_embeddings_cameraPose.shape[1]))
                elif self.relativeCameraPoseLoss_refType == "all_views":
                    video_embeddings_cameraPose = video_embeddings_cameraPose.reshape((B_actl * (self.num_views ** 2) * num_frames, video_embeddings_cameraPose.shape[3]))
                    video_embeddings_cameraPose = self.pose_lin(video_embeddings_cameraPose)
                    video_embeddings_cameraPose = video_embeddings_cameraPose.reshape((B_actl * (self.num_views ** 2), num_frames, video_embeddings_cameraPose.shape[1]))
            else:
                raise NotImplementedError

            return video_embeddings, video_embeddings_cameraPose
        else:
            video_embeddings = self.model(x)              # (batch_size, n_clsses)

            if self.use_egovlpV2_patchLevelVisualFeats:
                video_embeddings = video_embeddings.permute((0, 1, 4, 2, 3))
                B, num_frames = video_embeddings.shape[0], video_embeddings.shape[1]
                video_embeddings = video_embeddings.reshape((B * num_frames,
                                                             video_embeddings.shape[2],
                                                             video_embeddings.shape[3],
                                                             video_embeddings.shape[4],))

                video_embeddings = self.conv_egovlpV2_patchLevelVisualFeats(video_embeddings)
                video_embeddings = video_embeddings.reshape((B, -1))
            else:
                if self.egovlpV2_encodeWdinoV2:
                    video_embeddings = self.egovlpV2_encodeWdinoV2_x_norm_clstoken_agg(video_embeddings)

            return video_embeddings
        