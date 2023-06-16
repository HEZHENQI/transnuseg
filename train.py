import copy
import logging
import math
from os.path import join as pjoin
import cv2
import torch
import torch.nn as nn
import numpy as np
from torch.nn import CrossEntropyLoss, Dropout, Softmax, Linear, Conv2d, LayerNorm
from torch.nn.modules.utils import _pair
from scipy import ndimage
from torchvision import transforms
import torch
import torch.nn as nn
import torch.utils.checkpoint as checkpoint
from einops import rearrange
from timm.models.layers import DropPath, to_2tuple, trunc_normal_
import os
from torch.utils.data import Dataset,DataLoader,TensorDataset
from torch.nn.modules.loss import CrossEntropyLoss
import torch.optim as optim
from torchvision import transforms
import torch.utils.data as data
import scipy.io as sio
import matplotlib.pyplot as plt
import random
import time
import sys
from datetime import datetime
import argparse
from ray import tune
from ray.tune import CLIReporter
from ray.tune.schedulers import ASHAScheduler
import torch.optim as optim
from dataset import Histology,MyDataset
from utils import *
from models.transnucseg import TransNucSeg


device = 'cuda:0' if torch.cuda.is_available() else 'cpu'

num_classes = 2
# IMG_SIZE = 512
# PATCH_SIZE = 4
# IN_CHANS = 1
# EMBED_DIM = 96
# DEPTHS = [2, 2, 2, 2]
# NUM_HEADS = [3, 6, 12, 24]
# WINDOW_SIZE = 8 #original 7 --> 8
# MLP_RATIO = 4
# QKV_BIAS = True
# QK_SCALE = None
# DROP_RATE = 0.0
# DROP_PATH_RATE = 0.1
# APE = False
# PATCH_NORM = True 
# USE_CHECKPOINT = False
# PRETRAIN_CKPT = None


base_lr = 0.0005
WARMUP_LR = 5e-7
MIN_LR = 5e-6
batch_size = 2
num_epoch = 300
IMG_SIZE = 512

def main():
    '''
    model_type:  default: transnucseg
    alpha: ratio of the loss of nuclei segmentation, dafault=0.3
    beta: ratio of the loss of normal edge segmentation, dafault=0.35
    gamma: ratio of the loss of cluster edge segmentation, dafault=0.35
    sharing_ratio: ratio of sharing proportion of decoders, default=0.5
    dataset: Radiology(grayscale) or Histology(rgb), default=Histology
    model_path: if used pretrained model, put the path to the pretrained model here
    '''

    parser = argparse.ArgumentParser()
    parser.add_argument('--model_type', default="swin-unet-modified1") ### options : swin-unet, swin-unet-modified1,swin-unet-modified2
    parser.add_argument("--alpha",default=0.3)
    parser.add_argument("--beta",default=0.35)
    parser.add_argument("--gamma",default=0.35)
    parser.add_argument("--sharing_ratio",default=0.5)
    parser.add_argument("--dataset",default="Histology")
    parser.add_argument("--model_path",default="")

    args = parser.parse_args()
    
    model_type = args.model_type
    dataset = args.dataset

    alpha = float(args.alpha)
    beta = float(args.beta)
    gamma = float(args.gamma)
    sharing_ratio = float(args.sharing_ratio)

    if dataset == "Radiology":
        channel = 1
    elif dataset == "Histology":
        channel = 3
        
    else:
        logging.info("Wrong Dataset type")
        return 0
    
    
    
    
    model = TransNucSeg(img_size=IMG_SIZE)
  
 
    model.to(device)

    now = datetime.now()
    create_dir('./log')
    logging.basicConfig(filename='./log/log_{}_{}_{}.txt'.format(model_type,dataset,str(now)), level=logging.INFO,
                            format='[%(asctime)s.%(msecs)03d] %(message)s', datefmt='%H:%M:%S')
    logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
    logging.info("Batch size : {} , epoch num: {}, alph: {}, beta : {}, gamma: {}, sharing_ratio = {}".format(batch_size,num_epoch,alpha,beta,gamma,sharing_ratio))

    
    if dataset == "Radiology":
        total_data = MyDataset()
        train_set_size = int(len(total_data) * 0.8)
        test_set_size = len(total_data) - train_set_size

        train_set, test_set = data.random_split(total_data, [train_set_size, test_set_size],generator=torch.Generator().manual_seed(666))
    elif dataset == "Histology":
        data_path = "/root/autodl-tmp/Swin_unet/dataset/histology/histology_train"
        train_set = Histology(dir_path = os.path.join(data_path,"train"),transform = None)
        test_set = Histology(dir_path = os.path.join(data_path,"test"),transform = None)
        # logging.info("train size {} test size {}".format(train_set_size,test_set_size))

        # train_set, test_set = data.random_split(total_data, [train_set_size, test_set_size],generator=torch.Generator().manual_seed(21))
        
    else:
        logging.info("Wrong Dataset type")
        return 0

    trainloader = torch.utils.data.DataLoader(train_set, batch_size=batch_size, shuffle=True)
    testloader = torch.utils.data.DataLoader(test_set, batch_size=batch_size, shuffle=False)
 
    dataloaders = {"train":trainloader,"test":testloader}
    dataset_sizes = {"train":len(trainloader),"test":len(testloader)}
    logging.info("size train : {}, size test {} ".format(dataset_sizes["train"],dataset_sizes["test"]))
        
    test_loss = []
    train_loss = []
    lr_lists = []
        
    
    
    ce_loss1 = CrossEntropyLoss()
    dice_loss1 = DiceLoss(num_classes)
    ce_loss2 = CrossEntropyLoss()
    dice_loss2 = DiceLoss(num_classes)
    ce_loss3 = CrossEntropyLoss()
    dice_loss3 = DiceLoss(num_classes)
    dice_loss_dis = DiceLoss(num_classes)


  

    optimizer = optim.Adam(model.parameters(), lr=base_lr)
  

    best_loss = 100
    best_epoch = 0


    for epoch in range(num_epoch):
        # early stop, if the loss does not decrease for 50 epochs
        if epoch > best_epoch + 50:
            break
        for phase in ['train','test']:
            running_loss = 0
            running_loss_wo_dis = 0
            running_loss_seg = 0
            s = time.time()  # start time for this epoch
            if phase == 'train':
                model.train()  # Set model to training mode
            else:
                model.eval()   

            for i, d in enumerate(dataloaders[phase]):
              
                img, instance_seg_mask, semantic_seg_mask,normal_edge_mask,cluster_edge_mask = d
             
                img = img.float()    
                img = img.to(device)
                instance_seg_mask = instance_seg_mask.to(device)
                semantic_seg_mask = semantic_seg_mask.to(device)
                
                semantic_seg_mask2 = semantic_seg_mask.cpu().detach().numpy()
                normal_edge_mask2 = normal_edge_mask.cpu().detach().numpy()
                cluster_edge_mask2 = cluster_edge_mask.cpu().detach().numpy()
                cv2.imwrite('./saved/train_predicted_seg_mask.png',semantic_seg_mask2[0]*255)
                cv2.imwrite('./saved/train_predicted_norm_mask.png',normal_edge_mask2[0]*255)
                cv2.imwrite('./saved/train_predicted_cluster_mask.png',cluster_edge_mask2[0]*255)
                cluster_edge_mask = cluster_edge_mask.to(device)
                # print('img shape ',img.shape)
                # print('semantic_seg_mask shape ',semantic_seg_mask.shape)
                

                output1,output2,output3 = model(img)
                # output1_ce = torch.permute(output1,(1,0,2,3))
                # output2_ce = torch.permute(output2,(1,0,2,3))
                # output3_ce = torch.permute(output3,(1,0,2,3))
                # print('output1_ce shape ',output1_ce.shape)
                loss_seg = 0.4*ce_loss1(output1, semantic_seg_mask.long( )) + 0.6*dice_loss1(output1, semantic_seg_mask.float(), softmax=True)
                loss_nor = 0.4*ce_loss2(output2, normal_edge_mask.long()) + 0.6*dice_loss2(output2, normal_edge_mask.float(), softmax=True)
                loss_clu = 0.4*ce_loss3(output3, cluster_edge_mask.long()) + 0.6*dice_loss3(output3, cluster_edge_mask.float(), softmax=True)
                print("loss_seg {}, loss_nor {}, loss_clu {}".format(loss_seg,loss_nor,loss_clu))
                if epoch < 10:
                    ratio_d = 1
                elif epoch < 20:
                    ratio_d = 0.7
                elif epoch < 30:
                    ratio_d = 0.3
                elif epoch < 40:
                    ratio_d = 0.1
                # elif epoch >= 40:
                #     ratio_d = 0
                else:
                    ratio_d = 0
                
                ### calculating the distillation loss
                m = torch.softmax(output1, dim=1)
                # print('m.shape',m.shape)
                m = torch.argmax(m, dim=1)
                # print('m.shape',m.shape)
                # m = m.squeeze(0)
                m = m.cpu().detach().numpy()
                
                cv2.imwrite('./saved/train_predicted_m.png',m[0]*255)
                cv2.imwrite('./saved/train_predicted_m2.png',m[0])
                b = torch.argmax(torch.softmax(output2, dim=1), dim=1)
                
                
                b2 = b.cpu().detach().numpy()
                # print('b2 shape',b2.shape)
                cv2.imwrite('./saved/train_predicted_b.png',b2[0]*255)
                cv2.imwrite('./saved/train_predicted_b2.png',b2[0])
                
                c = torch.argmax(torch.softmax(output3, dim=1), dim=1)
                pred_edge_1 = edge_detection(m.copy(),channel)
                pred_edge_1 = torch.tensor(pred_edge_1).to(device)
                pred_edge_2 = output2-output3
                pred_edge_2[pred_edge_2<0] = 0
                
                
                # print("pred_edge_1 shape ",pred_edge_1.shape)
                # print("pred_edge_2 shape ",pred_edge_2.shape)
                dis_loss = dice_loss_dis(pred_edge_2,pred_edge_1.float())
                
                ### calculating total loss
                loss = alpha*loss_seg + beta*loss_nor + gamma*loss_clu + ratio_d*dis_loss

                running_loss+=loss.item()
                running_loss_wo_dis += (alpha*loss_seg + beta*loss_nor + gamma*loss_clu).item() ## Loss without distillation loss
                running_loss_seg += loss_seg.item() ## Loss for nuclei segmantation
            if phase == 'train':
                with torch.autograd.set_detect_anomaly(True):
                    optimizer.zero_grad()
                    loss.backward()
                    optimizer.step()
                
            e = time.time()
            epoch_loss = running_loss / dataset_sizes[phase]
            epoch_loss_wo_dis = running_loss_wo_dis / dataset_sizes[phase] ## Epoch Loss without distillation loss
            epoch_loss_seg = running_loss_seg / dataset_sizes[phase]       ## Epoch Loss for nuclei segmantation
            logging.info('Epoch {},: loss {}, {},time {}'.format(epoch+1,  epoch_loss,phase,e-s))
            logging.info('Epoch {},: loss without distillation {}, {},time {}'.format(epoch+1,  epoch_loss_wo_dis,phase,e-s))
            logging.info('Epoch {},: loss seg {}, {},time {}'.format(epoch+1,  epoch_loss_seg,phase,e-s))

            if phase == 'train':
                train_loss.append(epoch_loss)
            else:
                test_loss.append(epoch_loss)

            if phase == 'test' and epoch_loss_seg < best_loss:
                best_loss = epoch_loss
                best_epoch = epoch+1
                best_model_wts = copy.deepcopy(model.state_dict())
                logging.info("Best val loss {} save at epoch {}".format(best_loss,epoch+1))

    draw_loss(train_loss,test_loss,str(now))
    
    create_dir('./saved')
    torch.save(best_model_wts, './saved/model_epoch:{}_testloss:{}_{}.pt'.format(best_epoch,best_loss,str(now)))
    logging.info('Model saved. at {}'.format('./saved/model_spoch:{}_testloss:{}_{}.pt'.format(best_epoch,best_loss,str(now))))


    
    model.load_state_dict(best_model_wts)
    model.eval()

    dice_acc_test = 0
    dice_loss_test = DiceLoss(num_classes)
    dice_acc_test2 = 0
    dice_loss_test2 = DiceLoss(num_classes)
    F1 = 0
    F1_2 = 0
    acc = 0
    acc2 = 0
    Iou = 0
    aji = 0
    aji_smooth = 0
    ajip_smooth = 0
    aji2 = 0
    ajip = 0
    iou2 = 0
    pq1 = 0
    pq2 = 0
    pq3 = 0
    with torch.no_grad():
        for i, d in enumerate(testloader, 0):
            img, instance_seg_mask, semantic_seg_mask,normal_edge_mask,cluster_edge_mask = d
            semantic_seg_mask2 = semantic_seg_mask.cpu().detach().numpy()
            normal_edge_mask2 = normal_edge_mask.cpu().detach().numpy()
            cluster_edge_mask2 = cluster_edge_mask.cpu().detach().numpy()
            # img = img.unsqueeze(0)
            img = img.float()    
            img = img.to(device)

            # semantic_seg_mask = semantic_seg_mask.unsqueeze(0).float()
            
            
            output1,output2,output3 = model(img)
            d_l = dice_loss_test(output1, semantic_seg_mask.float(), softmax=True)
            dice_acc_test += 1- d_l.item()
                
            print("semantic_seg_mask shape",semantic_seg_mask.shape)
            # semantic_seg_mask = semantic_seg_mask.squeeze(0).detach().cpu().numpy()
            # instance_seg_mask = instance_seg_mask.squeeze(0).detach().cpu().numpy()


            m = torch.argmax(torch.softmax(output1, dim=1), dim=1)
            m = m.cpu().detach().numpy()
            cv2.imwrite('./saved/test_predicted_m.png',m[0]*255)

            b = torch.argmax(torch.softmax(output2, dim=1), dim=1)
            b = b.cpu().detach().numpy()

            c = torch.argmax(torch.softmax(output3, dim=1), dim=1)
            c = c.cpu().detach().numpy()


            result = m.copy() + b.copy() + c.copy()

            
            # ins_predict_smooth = sem2ins_smooth(m.copy(),b.copy(),c.copy())
            
            try:
                Iou += float(get_iou(m.copy(),semantic_seg_mask))
            except:
                Iou += 0
            try:
                F1 += float( calculate_F1_score(result,semantic_seg_mask))
            except:
                F1 += 0
            try:
                acc += float(calculate_acc(result,semantic_seg_mask))
            except:
                acc += 0
            # logging.info("{}th iou {}, iou_sum {}".format(i,get_iou(result,semantic_seg_mask),Iou))
            # F1 += calculate_F1_score(result,semantic_seg_mask)
            # acc += calculate_acc(result,semantic_seg_mask)
       

     
    
    logging.info("dice_acc {}".format(dice_acc_test/dataset_sizes['test']))
    logging.info("F1 {}".format(F1/dataset_sizes['test']))
    logging.info("acc {}".format(acc/dataset_sizes['test']))
    logging.info("IOU {}".format(Iou/dataset_sizes['test']))

  
if __name__=='__main__':
    main()