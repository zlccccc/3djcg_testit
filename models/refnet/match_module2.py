import torch
import torch.nn as nn
import torch.nn.functional as F
from models.transformer.attention import MultiHeadAttention
from models.transformer.utils import PositionWiseFeedForward
import random


class MatchModule(nn.Module):
    def __init__(self, num_proposals=256, lang_size=256, hidden_size=128, lang_num_size=300, det_channel=128, head=4, depth=2):
        super().__init__()
        self.use_box_embedding = True
        self.use_dist_weight_matrix = True
        self.use_obj_embedding = False

        self.num_proposals = num_proposals
        self.lang_size = lang_size
        self.hidden_size = hidden_size
        self.relation_depth = 1
        self.depth = depth-1

        self.features_concat = nn.Sequential(
            nn.Conv1d(det_channel, hidden_size, 1),
            nn.BatchNorm1d(hidden_size),
            nn.PReLU(hidden_size),
            nn.Conv1d(hidden_size, hidden_size, 1),
        )
        self.match = nn.Sequential(
            nn.Conv1d(hidden_size, hidden_size, 1),
            nn.BatchNorm1d(hidden_size),
            nn.PReLU(),
            nn.Conv1d(hidden_size, hidden_size, 1),
            nn.BatchNorm1d(hidden_size),
            nn.PReLU(),
            nn.Conv1d(hidden_size, 1, 1)
        )

        self.self_attn = nn.ModuleList(
            MultiHeadAttention(d_model=hidden_size, d_k=hidden_size // head, d_v=hidden_size // head, h=head) for i in range(depth))
        self.cross_attn = nn.ModuleList(
            MultiHeadAttention(d_model=hidden_size, d_k=hidden_size // head, d_v=hidden_size // head, h=head) for i in range(depth))  # k, q, v

        self.bbox_embedding = nn.Linear(12, 128)
        self.obj_embedding = nn.Linear(128, hidden_size)

        self.self_attn_fc = nn.Sequential(  # 4 32 32 4(head)
            nn.Linear(4, 32),  # xyz, dist
            nn.ReLU(),
            nn.LayerNorm(32),
            nn.Linear(32, 32),
            nn.ReLU(),
            nn.LayerNorm(32),
            nn.Linear(32, 4)
        )

    def forward(self, data_dict):
        """
        Args:
            xyz: (B,K,3)
            features: (B,C,K)
        Returns:
            scores: (B,num_proposal,2+3+NH*2+NS*4)
        """
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
            dist_weights = 1 / (dist+1e-2)
            norm = torch.sum(dist_weights, dim=2, keepdim=True)
            dist_weights = dist_weights / norm

            zeros = torch.zeros_like(dist_weights)
            #dist_weights = torch.cat([dist_weights, -dist, zeros, zeros], dim=1).detach()
            #dist_weights = torch.cat([-dist, -dist, zeros, zeros], dim=1).detach()
            #dist_weights = torch.cat([-dist, -dist, -dist, zeros], dim=1).detach()

            weights = torch.cat([center_dist, dist.permute(0, 2, 3, 1)], dim=-1).detach()  # K N N 4
            dist_weights = self.self_attn_fc(weights).permute(0, 3, 1, 2)
            attention_matrix_way = 'add'
        else:
            dist_weights = None
            attention_matrix_way = 'mul'

        # dist_weights = None

        # object size embedding
        features = data_dict['pred_bbox_feature'].permute(0, 2, 1)
        # B, N = features.shape[:2]
        features = self.features_concat(features).permute(0, 2, 1)
        batch_size, num_proposal = features.shape[:2]

        if self.use_obj_embedding:
            obj_feat = data_dict["point_clouds"][..., 6:6 + 128].permute(0, 2, 1)
            obj_feat_dim = obj_feat.shape[1]
            obj_feat_id_seed = data_dict["seed_inds"]
            obj_feat_id_seed = obj_feat_id_seed.long() + ((torch.arange(batch_size) * obj_feat.shape[1])[:, None].to(obj_feat_id_seed.device))
            obj_feat_id_seed = obj_feat_id_seed.reshape(-1)
            obj_feat_id_vote = data_dict["aggregated_vote_inds"]
            obj_feat_id_vote = obj_feat_id_vote.long() + ((torch.arange(batch_size) * data_dict["seed_inds"].shape[1])[:, None].to(obj_feat_id_vote.device))
            obj_feat_id_vote = obj_feat_id_vote.reshape(-1)
            obj_feat_id = obj_feat_id_seed[obj_feat_id_vote]
            obj_feat = obj_feat.reshape(-1, obj_feat_dim)[obj_feat_id].reshape(batch_size, num_proposal, obj_feat_dim)
            obj_embedding = self.obj_embedding(obj_feat)
            features = features + obj_embedding * 0.1

        # box embedding
        if self.use_box_embedding:
            bbox_corner = data_dict['pred_bbox_corner']
            bbox_center = bbox_corner.mean(dim=-2)
            bbox_size = (bbox_corner[:, :, -1, :] - bbox_corner[:, :, 0, :])
            # attention weight
            manual_bbox_feat = torch.cat([bbox_center, bbox_corner[:, :, 0, :], bbox_corner[:, :, -1, :], bbox_size], dim=-1).float()
            bbox_embedding = self.bbox_embedding(manual_bbox_feat)
            # print(objects_center.shape, '<< centers shape', flush=True)
            features = features + bbox_embedding
            # features = data_dict['aggregated_vote_features']  # batch_size, num_proposal, 128
        batch_size, num_proposal = features.shape[:2]

        objectness_masks = data_dict['objectness_scores'].max(2)[1].float().unsqueeze(2)  # batch_size, num_proposals, 1

        #features = self.mhatt(features, features, features, proposal_masks)
        features = self.self_attn[0](features, features, features, attention_weights=dist_weights, way=attention_matrix_way)

        len_nun_max = data_dict["lang_feat_list"].shape[1]

        #objectness_masks = objectness_masks.permute(0, 2, 1).contiguous()  # batch_size, 1, num_proposals
        data_dict["random"] = random.random()

        # copy paste
        feature0 = features.clone()
        if data_dict["istrain"][0] == 1 and data_dict["random"] < 0.5:
            obj_masks = objectness_masks.bool().squeeze(2)  # batch_size, num_proposals
            obj_lens = torch.zeros(batch_size, dtype=torch.int).cuda()
            for i in range(batch_size):
                obj_mask = torch.where(obj_masks[i, :] == True)[0]
                obj_len = obj_mask.shape[0]
                obj_lens[i] = obj_len

            obj_masks_reshape = obj_masks.reshape(batch_size*num_proposal)
            obj_features = features.reshape(batch_size*num_proposal, -1)
            obj_mask = torch.where(obj_masks_reshape[:] == True)[0]
            total_len = obj_mask.shape[0]
            obj_features = obj_features[obj_mask, :].repeat(2,1)  # total_len, hidden_size
            j = 0
            for i in range(batch_size):
                obj_mask = torch.where(obj_masks[i, :] == False)[0]
                obj_len = obj_mask.shape[0]
                j += obj_lens[i]
                if obj_len < total_len - obj_lens[i]:
                    feature0[i, obj_mask, :] = obj_features[j:j + obj_len, :]
                else:
                    feature0[i, obj_mask[:total_len - obj_lens[i]], :] = obj_features[j:j + total_len - obj_lens[i], :]


        feature1 = feature0[:, None, :, :].repeat(1, len_nun_max, 1, 1).reshape(batch_size*len_nun_max, num_proposal, -1)
        if dist_weights is not None:
            dist_weights = dist_weights[:, None, :, :, :].repeat(1, len_nun_max, 1, 1, 1).reshape(batch_size*len_nun_max, dist_weights.shape[1], num_proposal, num_proposal)

        lang_fea = data_dict["lang_fea"]
        # print("features", features.shape, lang_fea.shape)

        feature1 = self.cross_attn[0](feature1, lang_fea, lang_fea, data_dict["attention_mask"])

        for _ in range(self.depth):
            feature1 = self.self_attn[_+1](feature1, feature1, feature1, attention_weights=dist_weights, way=attention_matrix_way)
            feature1 = self.cross_attn[_+1](feature1, lang_fea, lang_fea, data_dict["attention_mask"])

        # print("feature1", feature1.shape)
        # match
        feature1_agg = feature1
        feature1_agg = feature1_agg.permute(0, 2, 1).contiguous()

        confidence1 = self.match(feature1_agg).squeeze(1)  # batch_size, num_proposals
        # print("confidence1", confidence1.shape)
        data_dict["cluster_ref"] = confidence1

        return data_dict


