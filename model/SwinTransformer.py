import torch
from torch import nn, einsum
from math import sqrt
from einops.layers.torch import Rearrange
from einops import rearrange
from timm.models.layers import to_2tuple, trunc_normal_, DropPath
from utils.Pos_Encode import get_relative_position_index
from utils.Mask import get_sw_mask
from utils.Window import window_encode, window_decode

class Embed_Encoder(nn.Module):
	def __init__(self, img_size, patch_size, img_channel, embed_dim):
		super().__init__()

		self.img_size = to_2tuple(img_size)
		self.patch_size = to_2tuple(patch_size)

		img_h, img_w = self.img_size
		patch_h, patch_w = self.patch_size

		assert img_h % patch_h == 0 and img_w % patch_w == 0

		self.img_channel = img_channel
		self.embed_dim = embed_dim

		patch_h_num, patch_w_num = img_h // patch_h, img_w // patch_w
		self.patch_num = patch_h_num * patch_w_num

		self.embed_enc = nn.Sequential(
			nn.Conv2d(img_channel, embed_dim, kernel_size=self.patch_size, stride=self.patch_size),
			Rearrange('b c h w-> b (h w) c'),
		)
		
	def forward(self, x):
		(B, C, H, W) = x.shape
		assert H == self.img_size[0] and W == self.img_size[1]
		return self.embed_enc(x)

class Embed_Pooling(nn.Module):
	def __init__(self, input_size, output_size, input_dim, output_dim):
		super().__init__()
		self.input_size = to_2tuple(input_size)
		self.output_size = to_2tuple(output_size)
		self.input_dim = input_dim
		self.output_dim = output_dim
		
		input_h, input_w = self.input_size
		output_h, output_w = self.output_size

		assert input_h % output_h == 0 and input_w % output_w == 0
		patch_h, patch_w = input_h // output_h, input_w // output_w
		patch_size = (patch_h, patch_w)

		self.pooling = nn.Sequential(
			Rearrange('b (h w) c -> b c h w'),
			nn.Conv2d(input_dim, output_dim, kernel_size=patch_size, stride=patch_size),
			Rearrange('b c h w -> b (h w) c'),
		)

	def forward(self, x):
		(B, L, D) = x.shape
		assert L == self.input_size[0] * self.input_size[1] and D == self.input_dim
		return self.pooling(x)

class WindowAttention(nn.Module):
	def __init__(self, input_size, heads_num, window_size, shift_size, input_dim, output_dim=None, heads_dim=None, qkv_bias=True, dropout=0.0):
		super().__init__()
		heads_dim = heads_dim if heads_dim is not None else input_dim
		output_dim = output_dim if output_dim is not None else input_dim
		self.heads_dim = heads_dim
		self.heads_num = heads_num
		self.input_dim = input_dim
		self.output_dim = output_dim
		window_h, window_w = self.window_size = to_2tuple(window_size)
		self.shift_size = to_2tuple(shift_size)
		self.input_size = to_2tuple(input_size)
		self.embed_num = window_h * window_w
		
		pos_dim = (2 * window_h - 1) * (2 * window_w - 1)
		relative_position_index = get_relative_position_index(window_size).view(-1)
		self.register_buffer("relative_position_index", relative_position_index)
		self.relative_position_table = nn.Parameter(torch.empty(pos_dim, heads_num))
		trunc_normal_(self.relative_position_table, std=.02)

		self.W_qkv = nn.Linear(input_dim, heads_dim*heads_num*3, bias=qkv_bias)
		self.div_qkv = Rearrange('b embed_num (qkv heads_num input_dim) -> qkv b heads_num embed_num input_dim', heads_num=heads_num, qkv=3)
		self.reshape_pe = Rearrange("(attn_i attn_j) heads_num -> heads_num attn_i attn_j", attn_i=window_h*window_w)
		self.softmax = nn.Softmax(dim=-1)
		self.combine = Rearrange('b heads_num embed_num inner_dim -> b embed_num (heads_num inner_dim)', heads_num=heads_num)
		self.out = nn.Sequential(
		    nn.Linear(heads_dim*heads_num, output_dim), 
		    nn.Dropout(dropout)
		)

	def forward(self, x, mask=None):
		x = window_encode(x, self.input_size, self.window_size, self.shift_size)

		BW, N, D = x.shape
		window_h, window_w = self.window_size
		assert N == window_h * window_w and D == self.input_dim

		(q, k, v) = self.div_qkv(self.W_qkv(x))
		a = einsum('bnid, bnjd -> bnij', k, q) / sqrt(self.inner_dim)

		relative_position_bias = self.reshape_pe(self.relative_position_table[self.relative_position_index])
		a = a + relative_position_bias

		if mask is not None:
			nWindow = mask.shape[1]
			a = rearrange(a, "(b nWindow) nHeads attn_i attn_j -> b nWindow nHeads attn_i attn_j", nWindow=nWindow)
			a = a + mask
			a = rearrange(a, "b nWindow nHeads attn_i attn_j -> (b nWindow) nHeads attn_i attn_j", nWindow=nWindow)

		a = self.softmax(a)

		y = einsum('bnij,bnjk -> bnik', a, v)
		y = self.out(self.combine(y))

		y = window_decode(y, self.input_size, self.window_size, self.shift_size)

		return y

class Mlp(nn.Module):
	def __init__(self, input_dim, hidden_dim=None, output_dim=None, dropout=0.0):
		super().__init__()
		hidden_dim = hidden_dim if hidden_dim is not None else input_dim
		output_dim = output_dim if output_dim is not None else input_dim

		self.mlp = nn.Sequential(
			nn.Linear(input_dim, hidden_dim),
			nn.GELU(),
			nn.Dropout(dropout),
			nn.Linear(hidden_dim, output_dim),
			nn.Dropout(dropout)
		)
	
	def forward(self, x):
		return self.mlp(x)

class SwinTransformerBlock(nn.Module):
	def __init__(self, input_size, input_dim, window_size, shift_size, heads_num,
					output_dim=None, wsa_inner_dim=None, wsa_output_dim=None, mlp_inner_dim=None,
					wsa_dropout=0.0, mlp_dropout=0.0, droppath=0.0,
					qkv_bias=True):
		super().__init__()

		wsa_output_dim = wsa_output_dim if wsa_output_dim is not None else input_dim

		self.LN1 = nn.LayerNorm(input_dim)
		self.WSA = WindowAttention(input_size, heads_num, window_size, shift_size, input_dim, wsa_output_dim, wsa_inner_dim, qkv_bias, wsa_dropout)
		self.LN2 = nn.LayerNorm(wsa_output_dim)
		self.MLP = Mlp(wsa_output_dim, mlp_inner_dim, output_dim, mlp_dropout)
		self.DropPath = DropPath(droppath)

		mask = get_sw_mask(input_size, window_size, shift_size)
		self.register_buffer("mask", mask)
	
	def forward(self, x):
		x = x + self.DropPath(self.WSA(self.LN1(x), self.mask))
		x = x + self.DropPath(self.MLP(self.LN2(x)))

		return x
