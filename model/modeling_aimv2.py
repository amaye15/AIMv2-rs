from typing import Optional, Tuple, Union

import torch
from .configuration_aimv2 import AIMv2Config
from torch import nn
from torch.nn import functional as F
from transformers.modeling_outputs import BaseModelOutputWithNoAttention
from transformers.modeling_utils import PreTrainedModel

__all__ = ["AIMv2Model"]


def _get_1d_sincos_pos_embed_from_grid(
    embed_dim: int, pos: torch.Tensor
) -> torch.Tensor:
    omega = torch.arange(embed_dim // 2).float()
    omega /= embed_dim / 2.0
    omega = 1.0 / 10000**omega  # (D / 2,)
    pos = pos.reshape(-1)  # (M,)
    out = pos[:, None] * omega[None, :]  # (M, D / 2), outer product
    emb_sin, emb_cos = torch.sin(out), torch.cos(out)  # (M, D / 2)
    emb = torch.concatenate([emb_sin, emb_cos], dim=1)  # (M, D)
    return emb


def get_sincos_pos_embed(h: int, w: int, embed_dim: int) -> torch.Tensor:
    assert embed_dim % 2 == 0, embed_dim
    grid_h = torch.arange(h).float()
    grid_w = torch.arange(w).float()
    grid = torch.meshgrid(grid_w, grid_h, indexing="xy")
    grid = torch.stack(grid, dim=0)
    grid = grid.reshape([2, 1, h, w])
    emb_h = _get_1d_sincos_pos_embed_from_grid(embed_dim // 2, grid[0])
    emb_w = _get_1d_sincos_pos_embed_from_grid(embed_dim // 2, grid[1])
    pos_embed = torch.concatenate([emb_h, emb_w], dim=1)  # (H * W, D)
    return pos_embed


class RMSNorm(nn.Module):
    def __init__(self, dim: int, eps: float = 1e-6):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(dim))
        self.eps = eps

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        output = self._norm(x.float()).type_as(x)
        return output * self.weight

    def extra_repr(self) -> str:
        return f"{tuple(self.weight.shape)}, eps={self.eps}"

    def _norm(self, x: torch.Tensor) -> torch.Tensor:
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps)


class AIMv2SwiGLUFFN(nn.Module):
    def __init__(self, config: AIMv2Config):
        super().__init__()
        hidden_features = config.intermediate_size
        in_features = config.hidden_size
        bias = config.use_bias

        self.fc1 = nn.Linear(in_features, hidden_features, bias=bias)
        self.fc2 = nn.Linear(hidden_features, in_features, bias=bias)
        self.fc3 = nn.Linear(in_features, hidden_features, bias=bias)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = F.silu(self.fc1(x)) * self.fc3(x)
        x = self.fc2(x)
        return x


class AIMv2PatchEmbed(nn.Module):
    def __init__(self, config: AIMv2Config):
        super().__init__()
        self.proj = nn.Conv2d(
            config.num_channels,
            config.hidden_size,
            kernel_size=(config.patch_size, config.patch_size),
            stride=(config.patch_size, config.patch_size),
        )
        self.norm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.proj(x).flatten(2).transpose(1, 2)
        x = self.norm(x)
        return x


class AIMv2ViTPreprocessor(nn.Module):
    def __init__(self, config: AIMv2Config):
        super().__init__()
        self.patch_h = config.patch_size
        self.patch_w = config.patch_size
        self.embed_dim = config.hidden_size

        self.patchifier = AIMv2PatchEmbed(config)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        _, _, H, W = x.shape
        tokens = self.patchifier(x)
        pos_embed = get_sincos_pos_embed(
            H // self.patch_h, W // self.patch_w, embed_dim=self.embed_dim
        )
        tokens = tokens + pos_embed.to(tokens.device)
        return tokens


class AIMv2Attention(nn.Module):
    def __init__(self, config: AIMv2Config):
        super().__init__()
        dim = config.hidden_size

        self.num_heads = config.num_attention_heads
        self.qkv = nn.Linear(dim, dim * 3, bias=config.qkv_bias)
        self.attn_drop = nn.Dropout(config.attention_dropout)
        self.proj = nn.Linear(dim, dim, bias=config.use_bias)
        self.proj_drop = nn.Dropout(config.projection_dropout)

    def forward(
        self, x: torch.Tensor, mask: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        B, N, C = x.shape
        qkv = (
            self.qkv(x)
            .reshape(B, N, 3, self.num_heads, C // self.num_heads)
            .permute(2, 0, 3, 1, 4)
        )
        q, k, v = qkv.unbind(0)

        x = F.scaled_dot_product_attention(q, k, v, attn_mask=mask)
        x = x.transpose(1, 2).contiguous().reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class AIMv2Block(nn.Module):
    def __init__(self, config: AIMv2Config):
        super().__init__()
        self.attn = AIMv2Attention(config)
        self.norm_1 = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.mlp = AIMv2SwiGLUFFN(config)
        self.norm_2 = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)

    def forward(
        self, x: torch.Tensor, mask: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        x = x + self.attn(self.norm_1(x), mask)
        x = x + self.mlp(self.norm_2(x))
        return x


class AIMv2Transformer(nn.Module):
    def __init__(self, config: AIMv2Config):
        super().__init__()
        self.blocks = nn.ModuleList(
            [AIMv2Block(config) for _ in range(config.num_hidden_layers)]
        )
        self.post_trunk_norm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)

    def forward(
        self,
        tokens: torch.Tensor,
        mask: Optional[torch.Tensor] = None,
        output_hidden_states: bool = False,
    ) -> Tuple[torch.Tensor, Optional[Tuple[torch.Tensor, ...]]]:
        hidden_states = () if output_hidden_states else None
        for block in self.blocks:
            tokens = block(tokens, mask)
            if output_hidden_states:
                hidden_states += (tokens,)
        tokens = self.post_trunk_norm(tokens)
        return tokens, hidden_states


class AIMv2PretrainedModel(PreTrainedModel):
    config_class = AIMv2Config
    base_model_prefix = "aimv2"
    main_input_name = "pixel_values"
    _no_split_modules = ["AIMv2ViTPreprocessor", "AIMv2Block"]
    _supports_sdpa = True


class AIMv2Model(AIMv2PretrainedModel):
    def __init__(self, config: AIMv2Config):
        super().__init__(config)
        self.preprocessor = AIMv2ViTPreprocessor(config)
        self.trunk = AIMv2Transformer(config)

    def forward(
        self,
        pixel_values: torch.Tensor,
        mask: Optional[torch.Tensor] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[
        Tuple[torch.Tensor],
        Tuple[torch.Tensor, Tuple[torch.Tensor, ...]],
        BaseModelOutputWithNoAttention,
    ]:
        if output_hidden_states is None:
            output_hidden_states = self.config.output_hidden_states
        if return_dict is None:
            return_dict = self.config.use_return_dict

        x = self.preprocessor(pixel_values)
        x, hidden_states = self.trunk(
            x, mask, output_hidden_states=output_hidden_states
        )

        if not return_dict:
            res = (x,)
            res += (hidden_states,) if output_hidden_states else ()
            return res

        return BaseModelOutputWithNoAttention(
            last_hidden_state=x,
            hidden_states=hidden_states,
        )

