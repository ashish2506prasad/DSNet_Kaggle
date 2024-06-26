import logging

import numpy as np
import torch
from torch import nn

from anchor_based import anchor_helper
from anchor_based.dsnet import DSNet, DSNet_DeepAttention, DSNet_MultiAttention, DSNetTriangularAttention, DSNetMotionFeatures
from anchor_based.losses import calc_cls_loss, calc_loc_loss
from evaluate import evaluate
from helpers import data_helper, vsumm_helper, bbox_helper
import matplotlib.pyplot as plt

import time as time

logger = logging.getLogger()


def xavier_init(module):
    cls_name = module.__class__.__name__
    if 'Linear' in cls_name or 'Conv' in cls_name:
        nn.init.xavier_uniform_(module.weight, gain=np.sqrt(2.0))
        if module.bias is not None:
            nn.init.constant_(module.bias, 0.1)
  

def train(args, split, save_path):

    if args.model_depth == 'shallow':
        model = DSNet(base_model=args.base_model, num_feature=args.num_feature,
                    num_hidden=args.num_hidden, anchor_scales=args.anchor_scales,
                    num_head=args.num_head, fc_depth=args.fc_depth)
    elif args.model_depth == 'deep':
        model = DSNet_DeepAttention(base_model=args.base_model, num_feature=args.num_feature,
                    num_hidden=args.num_hidden, anchor_scales=args.anchor_scales,
                    num_head=args.num_head, fc_depth=args.fc_depth, attention_depth=args.attention_depth)
    elif args.model_depth == 'local-global-attention':
        model = DSNet_MultiAttention(base_model=args.base_model, num_feature=args.num_feature,
                    num_hidden=args.num_hidden, anchor_scales=args.anchor_scales,
                    num_head=args.num_head)
    elif args.model_depth == 'triangular':
        model = DSNetTriangularAttention(base_model=args.base_model, num_feature=args.num_feature,
                    num_hidden=args.num_hidden, anchor_scales=args.anchor_scales,
                    num_head=args.num_head)
    elif args.model_depth == 'cross-attention':
        model = DSNetMotionFeatures(base_model=args.base_model, num_feature=args.num_feature,
                    num_hidden=args.num_hidden, anchor_scales=args.anchor_scales,
                    num_head=args.num_head, attention_depth=args.attention_depth, encoder_type=args.encoder_type)
    model = model.to(args.device)

    model.apply(xavier_init)

    parameters = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.Adam(parameters, lr=args.lr,
                                 weight_decay=args.weight_decay)

    max_val_fscore = -1

    train_set = data_helper.VideoDataset(split['train_keys'])
    train_loader = data_helper.DataLoader(train_set, shuffle=True)

    val_set = data_helper.VideoDataset(split['test_keys'])
    val_loader = data_helper.DataLoader(val_set, shuffle=False)

    epoch_list = []
    f1_score = []

    for epoch in range(args.max_epoch):
        start = time.time()
        model.train()
        stats = data_helper.AverageMeter('loss', 'cls_loss', 'loc_loss')

        for _, seq, gtscore, cps, n_frames, nfps, picks, motion_featurs, _ in train_loader:
            keyshot_summ = vsumm_helper.get_keyshot_summ(
                gtscore, cps, n_frames, nfps, picks)
            target = vsumm_helper.downsample_summ(keyshot_summ)

            if not target.any():
                continue

            target_bboxes = bbox_helper.seq2bbox(target)
            target_bboxes = bbox_helper.lr2cw(target_bboxes)
            anchors = anchor_helper.get_anchors(target.size, args.anchor_scales)
            # Get class and location label for positive samples
            cls_label, loc_label = anchor_helper.get_pos_label(
                anchors, target_bboxes, args.pos_iou_thresh)

            # Get negative samples
            num_pos = cls_label.sum()
            cls_label_neg, _ = anchor_helper.get_pos_label(
                anchors, target_bboxes, args.neg_iou_thresh)
            cls_label_neg = anchor_helper.get_neg_label(
                cls_label_neg, int(args.neg_sample_ratio * num_pos))

            # Get incomplete samples
            cls_label_incomplete, _ = anchor_helper.get_pos_label(
                anchors, target_bboxes, args.incomplete_iou_thresh)
            cls_label_incomplete[cls_label_neg != 1] = 1
            cls_label_incomplete = anchor_helper.get_neg_label(
                cls_label_incomplete,
                int(args.incomplete_sample_ratio * num_pos))

            cls_label[cls_label_neg == -1] = -1
            cls_label[cls_label_incomplete == -1] = -1

            cls_label = torch.tensor(cls_label, dtype=torch.float32).to(args.device)
            loc_label = torch.tensor(loc_label, dtype=torch.float32).to(args.device)
            
            # load features in the model
            seq = torch.tensor(seq, dtype=torch.float32).unsqueeze(0).to(args.device)
            if motion_featurs is not None:
                motion_featurs = torch.tensor(motion_featurs, dtype=torch.float32).unsqueeze(0).to(args.device)
                pred_cls, pred_loc = model(seq, motion_featurs)
                # print(pred_loc)

            else:
                pred_cls, pred_loc = model(seq)
            

            loc_loss = calc_loc_loss(pred_loc, loc_label, cls_label)
            cls_loss = calc_cls_loss(pred_cls, cls_label)

            loss = cls_loss + args.lambda_reg * loc_loss

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()


            stats.update(loss=loss.item(), cls_loss=cls_loss.item(),
                         loc_loss=loc_loss.item())
        end = time.time()

        val_fscore, _ = evaluate(model, val_loader, args.nms_thresh, args.device)
        f1_score.append(val_fscore)
        epoch_list.append(epoch)

        if max_val_fscore < val_fscore:
            max_val_fscore = val_fscore
            torch.save(model.state_dict(), str(save_path))

        if epoch % 20 == 0:
            if args.where == 'local':
                logger.info(f'Epoch: {epoch}/{args.max_epoch} '
                            f'Loss: {stats.cls_loss:.4f}/{stats.loc_loss:.4f}/{stats.loss:.4f} '
                            f'F-score cur/max: {val_fscore:.4f}/{max_val_fscore:.4f}'
                            f'Time: {end-start:.4f}')
            else:
                print(f'Epoch: {epoch}/{args.max_epoch} '
                            f'Loss: {stats.cls_loss:.4f}/{stats.loc_loss:.4f}/{stats.loss:.4f} '
                            f'F-score cur/max: {val_fscore:.4f}/{max_val_fscore:.4f}'
                            f'Time: {end-start:.4f}')

    return max_val_fscore, f1_score, epoch_list
