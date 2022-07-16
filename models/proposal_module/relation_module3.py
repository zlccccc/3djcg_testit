import torch
import torch.nn as nn
import torch.nn.functional as F
from models.transformer.attention import MultiHeadAttention
from models.transformer.utils import PositionWiseFeedForward
import random


class RelationModule(nn.Module):
    def __init__(self, num_proposals=256, hidden_size=128, lang_num_size=300, det_channel=128, head=4, depth=2, num_class=100):
        super().__init__()
        self.use_box_embedding = True
        self.use_class_embedding = True
        self.use_dist_weight_matrix = True

        self.num_proposals = num_proposals
        self.hidden_size = hidden_size
        self.depth = depth

        self.features_concat = nn.Sequential(
            nn.Conv1d(det_channel, hidden_size, 1),
            nn.BatchNorm1d(hidden_size),
            nn.PReLU(hidden_size),
            nn.Conv1d(hidden_size, hidden_size, 1),
        )
        self.self_attn_fc = nn.Sequential(  # 4 128 256 4(head)
            nn.Linear(5, 32),  # xyz, dist, 1/dist
            nn.ReLU(),
            nn.LayerNorm(32),
            nn.Linear(32, 32),
            nn.ReLU(),
            nn.LayerNorm(32),
            nn.Linear(32, head)
        )
        self.self_attn = nn.ModuleList(
            MultiHeadAttention(d_model=hidden_size, d_k=hidden_size // head, d_v=hidden_size // head, h=head) for i in range(depth))

        self.class_embedding = nn.Linear(num_class, hidden_size)
        self.bbox_embedding = nn.Linear(27, hidden_size)  # corner + center


    def _get_bbox_centers(self, corners):
        coord_min = torch.min(corners, dim=2)[0] # batch_size, num_proposals, 3
        coord_max = torch.max(corners, dim=2)[0] # batch_size, num_proposals, 3
        return (coord_min + coord_max) / 2


    def forward(self, data_dict):
        """
        Args:
            xyz: (B,K,3)
            features: (B,C,K)
        Returns:
            scores: (B,num_proposal,2+3+NH*2+NS*4)
        """
        # Preprocess
        if self.use_dist_weight_matrix:
            # Attention Weight
            # objects_center = data_dict['center']
            objects_center = data_dict['pred_bbox_corner'].mean(dim=-2)
            N_K = objects_center.shape[1]
            center_A = objects_center[:, None, :, :].repeat(1, N_K, 1, 1)
            center_B = objects_center[:, :, None, :].repeat(1, 1, N_K, 1)
            center_dist = (center_A - center_B)
            dist = center_dist.pow(2)
            # print(dist.shape, '<< dist shape', flush=True)
            dist = torch.sqrt(torch.sum(dist, dim=-1))[:, None, :, :]
            dist_weights = 1 / (dist + 1e-2)
            norm = torch.sum(dist_weights, dim=2, keepdim=True)
            dist_weights = dist_weights / norm

            weights = torch.cat([center_dist, dist.permute(0, 2, 3, 1), dist_weights.permute(0, 2, 3, 1)],dim=-1).detach()  # K N N 4
            dist_weights = self.self_attn_fc(weights).permute(0, 3, 1, 2)

            attention_matrix_way = 'add'
        else:
            dist_weights = None
            attention_matrix_way = 'mul'

        # object size embedding
        features = data_dict['pred_bbox_feature'].permute(0, 2, 1)
        # B, N = features.shape[:2]
        features = self.features_concat(features).permute(0, 2, 1)

        batch_size, num_proposal = features.shape[:2]
        # box embedding
        if self.use_box_embedding:
            corners = data_dict['pred_bbox_corner']
            centers = self._get_bbox_centers(corners)  # batch_size, num_proposals, 3
            num_proposals = centers.shape[1]
            # attention weight
            manual_bbox_feat = torch.cat(
                [centers, (corners - centers[:, :, None, :]).reshape(batch_size, num_proposals, -1)], dim=-1).float()
            bbox_embedding = self.bbox_embedding(manual_bbox_feat)
            features = features + bbox_embedding

        if self.use_class_embedding:
            class_feat = data_dict['sem_cls_scores'].detach()
            class_embedding = self.class_embedding(class_feat)
            features = features + class_embedding

        objectness_masks = data_dict['objectness_scores'].max(2)[1].float().unsqueeze(2)  # batch_size, num_proposals, 1

        # features = self.mhatt(features, features, features, proposal_masks)
        for i in range(self.depth):
            features = self.self_attn[i](features, features, features, attention_weights=dist_weights,
                                         way=attention_matrix_way)

        data_dict['dist_weights'] = dist_weights
        data_dict['attention_matrix_way'] = attention_matrix_way
        data_dict["bbox_feature"] = features
        return data_dict

