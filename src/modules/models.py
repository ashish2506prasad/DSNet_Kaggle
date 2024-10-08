import math

import torch
from torch import nn
from transformer.nystroformer import NystromAttention
from modules.frequency_inspired.fourier_attention import FNet_layer
from transformer.performer import Performer
from modules.frequency_inspired.dwt_attention import DwtNet

    

class ScaledDotProductAttention(nn.Module):
    def __init__(self, d_k):
        super().__init__()
        self.dropout = nn.Dropout(0.5)
        self.sqrt_d_k = math.sqrt(d_k)

    def forward(self, Q, K, V):
        attn = torch.bmm(Q, K.transpose(2, 1))
        attn = attn / self.sqrt_d_k

        attn = torch.softmax(attn, dim=-1)
        attn = self.dropout(attn)
        y = torch.bmm(attn, V)

        return y, attn


class MultiHeadAttention(nn.Module):
    def __init__(self, num_head=8, num_feature=1024):
        super().__init__()
        self.num_head = num_head

        self.Q = nn.Linear(num_feature, num_feature, bias=False)
        self.K = nn.Linear(num_feature, num_feature, bias=False)
        self.V = nn.Linear(num_feature, num_feature, bias=False)

        self.d_k = num_feature // num_head
        self.attention = ScaledDotProductAttention(self.d_k)

        self.fc = nn.Sequential(
            nn.Linear(num_feature, num_feature, bias=False),
            nn.Dropout(0.5)
        )

    def forward(self, x):
        _, seq_len, num_feature = x.shape  # [1, seq_len, 1024]
        K = self.K(x)  # [1, seq_len, 1024]
        Q = self.Q(x)  # [1, seq_len, 1024]
        V = self.V(x)  # [1, seq_len, 1024]

        K = K.view(1, seq_len, self.num_head, self.d_k).permute(
            2, 0, 1, 3).contiguous().view(self.num_head, seq_len, self.d_k)
        Q = Q.view(1, seq_len, self.num_head, self.d_k).permute(
            2, 0, 1, 3).contiguous().view(self.num_head, seq_len, self.d_k)
        V = V.view(1, seq_len, self.num_head, self.d_k).permute(
            2, 0, 1, 3).contiguous().view(self.num_head, seq_len, self.d_k)

        y, attn = self.attention(Q, K, V)  # [num_head, seq_len, d_k]
        y = y.view(1, self.num_head, seq_len, self.d_k).permute(
            0, 2, 1, 3).contiguous().view(1, seq_len, num_feature)

        y = self.fc(y)

        return y, attn


class AttentionExtractor(MultiHeadAttention):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def forward(self, *inputs):
        out, _ = super().forward(*inputs)
        return out


class GCNExtractor(nn.Module):
    def __init__(self, num_feature):
        super().__init__()
        from torch_geometric.nn import GCNConv
        self.gcn = GCNConv(num_feature, num_feature)

    def forward(self, x):
        x = x.squeeze(0)
        edge_indices, edge_weights = self.create_graph(x, keep_ratio=0.3)
        out = self.gcn(x, edge_indices, edge_weights)
        out = out.unsqueeze(0)
        return out

    @staticmethod
    def create_graph(x, keep_ratio=0.3):
        seq_len, _ = x.shape
        keep_top_k = int(keep_ratio * seq_len * seq_len)

        edge_weights = torch.matmul(x, x.t())
        edge_weights = edge_weights - torch.eye(seq_len, seq_len).to(x.device)
        edge_weights = edge_weights.view(-1)
        edge_weights, edge_indices = torch.topk(
            edge_weights, keep_top_k, sorted=False)

        edge_indices = edge_indices.unsqueeze(0)
        edge_indices = torch.cat(
            [edge_indices / seq_len, edge_indices % seq_len])

        return edge_indices, edge_weights


class LSTMExtractor(nn.LSTM):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def forward(self, *inputs):
        out, _ = super().forward(*inputs)
        return out



def build_base_model(base_type: str,
                     num_feature: int,
                     num_head: int,
                     orientation: str = None
                     ) -> nn.Module:
    if base_type == 'linear':
        base_model = nn.Linear(num_feature, num_feature)
    elif base_type == 'lstm':
        base_model = LSTMExtractor(num_feature, num_feature)
    elif base_type == 'bilstm':
        base_model = LSTMExtractor(num_feature, num_feature // 2,
                                   bidirectional=True)
    elif base_type == 'gcn':
        base_model = GCNExtractor(num_feature)
    elif base_type == 'attention':
        base_model = AttentionExtractor(num_head, num_feature)
    elif base_type == 'nystromformer':
        base_model = NystromAttention(dim=num_feature, dim_head = 64, heads = num_head, num_landmarks = 64, pinv_iterations = 6,residual = True,residual_conv_kernel = 33)
    elif base_type == 'fourier':
        base_model = FNet_layer(num_feature, dropout=0.5, orientation=orientation)
    # elif base_type == 'linformer':
    #     base_model = Linformer(dim=num_feature, depth=1, heads=num_head, dim_head=64, seq_len=5000, k=1000, one_kv_head=False, share_kv=False, dropout=0.5, mlp_dim=1024)
    elif base_type == 'performer':
        base_model = Performer(dim=num_feature, depth=1, heads=num_head, mlp_dim=1024, dim_head=64, dropout=0.5)
    elif base_type == 'dwt':
        base_model = DwtNet(num_feature=num_feature, wavelet='haar', dropout=0.5)
    else:
        raise ValueError(f'Invalid base model {base_type}')

    return base_model
