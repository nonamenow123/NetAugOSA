# import apex.amp as amp
# from apex import amp
import torchvision
import argparse
import os
import numpy as np
import torch
import torch.optim as optim
import torch.utils.data as data
from sklearn.metrics import accuracy_score
from torch import nn
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.utils.data.sampler import WeightedRandomSampler
from tqdm import tqdm
from utility.initialize import initialize
from utility.statics_ViT import statics,calc_NetAug
from sklearn.model_selection import KFold,train_test_split
from sklearn.metrics import roc_curve, auc, f1_score
import matplotlib.pyplot as plt
import wandb
from Model.base.ResNet_baseV2 import ResNet_base, BasicBlock_baseV2
from Model.NetAug.ResNet18_NetAugV2 import NetAugResNet18
from dataloader.dataset_PIL_New_fix import OSAnpyDataset_augment_mixup_mix_3d_png
from dataloader.augment import SpecAug, jitter
from torchvision import transforms
from util.lr_scheduler import CosineLRwithWarmup


def parse_option():
    parser = argparse.ArgumentParser('OSA', add_help=False)
    #parser.add_argument('--project_name', type=str, default='OSA_NetAug', help="experiment name")
    parser.add_argument('--project_name', type=str, default='debug', help="experiment name")
    parser.add_argument('--experiment', type=str, default='0408_resnet18_quarterquarter_adam_1_retry2', help="experiment name")
    parser.add_argument('--NetAug', type=list[float], default=[1], help="[1] for no Aug")
    # easy config modification
    parser.add_argument('--lr', type=float, default=1e-3, help="lr")
    parser.add_argument('--num_epochs', type=int, default=100, help="epoch")
    parser.add_argument('--batch-size', type=int, default=128, help="batch size for single GPU")
    args = parser.parse_args()


    return args


def train(model, model_base, aug, device, train_loader, optimizer, optimizer_base, lr_scheduler, lr_scheduler_base, epoch, fold, wandb_logger=None):
    try:
        model.sort_channels()
        print('Sort Channel')
    except:
        print('No Sort Channel')
    # =========================================================
    # Initial Training parameters
    # =========================================================
    model.train()  # set training mode
    gt_labels = list()
    pred_labels_1x = list()
    pred_labels_base = list()
    pred_labels_Nx = list()
    train_loss_1x = 0.0
    train_loss_Nx = 0.0
    train_loss_base = 0.0
    loader = tqdm(train_loader)
    criterion = nn.CrossEntropyLoss().to(device)
    for batch_idx, (data, target, _C) in enumerate(loader):
        # =========================================================
        # Initial Weight&Bias
        # =========================================================
        data = data.float()
        data, target = data.to(device), target.to(device)
        model = model.to(device)
        model_base = model_base.to(device)
        optimizer.zero_grad()
        for lab in _C.detach().cpu().numpy():
            gt_labels.append(lab)
        # =========================================================
        # Training
        # =========================================================
        # output = model(data)
        # loss = criterion(output, target) # + 1.0 * label_smoothing(output, target)
        # train_loss += loss.item()
        # optimizer.zero_grad()
        # loss.backward()
        # optimizer.step()
        # =========================================================
        # Backward 1x model
        # =========================================================

        #model.set_active(mode="min")
        output = model(data)
        loss = criterion(output, target)
        train_loss_1x += loss.item()
        loss.backward()
        pred_1x = output.argmax(dim=1, keepdim=True)
        for lab in pred_1x.detach().cpu().numpy():
            pred_labels_1x.append(lab)
        # =========================================================
        # Backward base model
        # =========================================================

        # model.set_active(mode="min")
        output = model_base(data)
        loss = criterion(output, target)
        train_loss_base += loss.item()
        loss.backward()
        pred_base = output.argmax(dim=1, keepdim=True)
        for lab in pred_base.detach().cpu().numpy():
            pred_labels_base.append(lab)
        optimizer_base.step()
        lr_scheduler_base.step()
        # =========================================================
        # Backward Nx model
        # =========================================================
        if aug != False:
            #model.set_active(mode="random")
            output = model(data)
            aug_loss = criterion(output, target)
            train_loss_Nx += aug_loss.item()
            aug_loss.backward()
            pred_Nx = output.argmax(dim=1, keepdim=True)
            for lab in pred_Nx.detach().cpu().numpy():
                pred_labels_Nx.append(lab)
        # =========================================================
        # Initial Weight&Bias
        # =========================================================
        optimizer.step()
        lr_scheduler.step()
        lr_scheduler_base.step()
    acc_1x = accuracy_score(gt_labels, pred_labels_1x)
    acc_base = accuracy_score(gt_labels, pred_labels_base)
    train_loss_1x /= len(train_loader.sampler)
    if aug != False:
        acc_Nx = accuracy_score(gt_labels, pred_labels_Nx)
        train_loss_Nx /= len(train_loader.sampler)
        if wandb_logger != None:
            log_stats = {"Training loss 1x Model {}".format(fold): train_loss_1x,
                         "Training loss Nx Model{}".format(fold): train_loss_Nx,
                         "Training Acc 1x Model{}".format(fold): acc_1x,
                         "Training Acc Nx Model{}".format(fold): acc_Nx,}
            wandb_logger.log(log_stats)
        print(f'Train loss 1x : {train_loss_1x}'
              f'Train loss Nx : {train_loss_Nx}'
              f'Train Acc 1x : {acc_1x}'
              f'Train Acc Nx : {acc_Nx}after Epoch {epoch}')
    else:
        if wandb_logger != None:
            log_stats = {"Training loss 1x Model {}".format(fold): train_loss_1x,
                         "Training Acc 1x Model{}".format(fold): acc_1x,}
            wandb_logger.log(log_stats)
        print(f'Train loss 1x : {train_loss_1x}'
              f'Train Acc 1x : {acc_1x}after Epoch {epoch}'
              f'Train loss base : {train_loss_base}'
              f'Train Acc base : {acc_base}after Epoch {epoch}')


def test(model, model_base, aug, device, test_loader, fold, result, epoch, wandb_logger=None):
    # =========================================================
    # Initial Val parameters
    # =========================================================
    model.eval()
    model_base.eval()
    val_loss_1x = 0.0
    val_loss_base = 0.0
    val_loss_max = 0.0
    correct = 0
    total = 0
    gt_labels = list()
    pred_labels_1x = list()
    pred_labels_base = list()
    pred_labels_max = list()
    loader = tqdm(test_loader)
    criterion = nn.CrossEntropyLoss().to(device)
    with torch.no_grad():
        for batch_idx, (data, target, _C) in enumerate(loader):
            # =========================================================
            # Validation
            # =========================================================
            data = data.float()
            data, target = data.to(device), target.to(device)
            for lab in _C.detach().cpu().numpy():
                gt_labels.append(lab)
            # =========================================================
            # Backward 1x model
            # =========================================================
            #model.set_active(mode="min")
            output = model(data)
            loss = criterion(output, target)
            val_loss_1x += loss.item()
            pred_1x = output.argmax(dim=1, keepdim=True)
            for lab in pred_1x.detach().cpu().numpy():
                pred_labels_1x.append(lab)
            # =========================================================
            # Backward base model
            # =========================================================
            # model.set_active(mode="min")
            output = model_base(data)
            loss = criterion(output, target)
            val_loss_base += loss.item()
            pred_base = output.argmax(dim=1, keepdim=True)
            for lab in pred_base.detach().cpu().numpy():
                pred_labels_base.append(lab)
            # =========================================================
            # Backward max model
            # =========================================================
            if aug != False:
                #model.set_active(mode="max")
                output = model(data)
                aug_loss = criterion(output, target)
                val_loss_max += aug_loss.item()
                pred_Nx = output.argmax(dim=1, keepdim=True)
                for lab in pred_Nx.detach().cpu().numpy():
                    pred_labels_max.append(lab)
            total += _C.size(0)



    val_loss_1x /= len(test_loader.sampler)
    acc_1x = accuracy_score(gt_labels, pred_labels_1x)
    f1_1x = f1_score(gt_labels, pred_labels_1x, average='macro')
    acc_base = accuracy_score(gt_labels, pred_labels_base)
    f1_base = f1_score(gt_labels, pred_labels_base, average='macro')

    if aug != False:
        val_loss_max /= len(test_loader.sampler)
        acc_max = accuracy_score(gt_labels, pred_labels_max)
        f1_max = f1_score(gt_labels, pred_labels_max, average='macro')
        if wandb_logger != None:
            log_stats = {"Testing loss 1x {}".format(fold): val_loss_1x,
                         "Testing accuracy 1x {}".format(fold): acc_1x,
                         "Testing f1 1x {}".format(fold):f1_1x,
                         "Testing loss max {}".format(fold): val_loss_max,
                         "Testing accuracy max {}".format(fold): acc_max,
                         "Testing f1 max {}".format(fold): f1_max
                         }
            wandb_logger.log(log_stats)
        print(f'Train f1 1x : {f1_1x}'
              f'Train f1 max : {f1_max}'
              f'Train Acc 1x : {acc_1x}'
              f'Train Acc max : {acc_max}'
              f'Train loss 1x : {val_loss_1x}'
              f'Train loss max : {val_loss_max} after Epoch {epoch}')
    else:
        acc_max = acc_1x
        if wandb_logger != None:
            log_stats = {"Testing loss 1x {}".format(fold): val_loss_1x,
                         "Testing accuracy 1x {}".format(fold): acc_1x,
                         "Testing f1 1x {}".format(fold): f1_1x,
                         }
            wandb_logger.log(log_stats)
        print(f'Train f1 1x : {f1_1x}'
              f'Train Acc 1x : {acc_1x}'
              f'Train loss 1x : {val_loss_1x} after Epoch {epoch}'
              f'Train f1 base : {f1_base}'
              f'Train Acc base : {acc_base}'
              f'Train loss base : {val_loss_base} after Epoch {epoch}')

    result[fold] = 100.0 * (correct / total)

    return (acc_max + acc_1x) / 2, result


def main(args):
    torch.manual_seed(3)
    torch.cuda.manual_seed(3)
    np.random.seed(3)
    tprs = []
    aucs = []
    num_class = 4
    initialize(seed=2018)
    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda:0" if use_cuda else "cpu")
    print("Use", device)
    #path = 'J:/DATASET/OSA/DataSet/Dataset_newmel_npy/train/'
    if args.NetAug != [1]:
        aug = True
    else:
        aug = False
    # =========================================================
    # build Full Dataset
    # =========================================================
    path = 'F:/Dataset_stick_png/train/'
    transform = transforms.Compose([SpecAug()])
    dataset = OSAnpyDataset_augment_mixup_mix_3d_png(path, train=True,  Transform_AUG=transform, ORI=True, SPLICE=False, COVER=False, MIXUP=False, doMixedFrquencyMasking=False, ReSort=False)
    dataset_test = OSAnpyDataset_augment_mixup_mix_3d_png(path, train=False, Transform_AUG=transform)
    experiment_name = args.experiment
    print(experiment_name)
    # log wandb
    wandb.login(key="08f61b4b727181d639dd88707852161052a9f1a4")
    dataset_size = len(dataset)
    print(dataset_size)
    # =========================================================
    # Initial Kfold
    # =========================================================
    k_folds = 5
    kfold = KFold(n_splits=k_folds, shuffle=True)
    result = {}
    print('--------------------------------')
    fig, ax = plt.subplots()
    # =========================================================
    # Splice Fold
    # =========================================================
    for fold, (train_ids, test_ids) in enumerate(kfold.split(dataset)):
        print(f'FOLD {fold}')
        print('--------------------------------')
        # =========================================================
        # build Model
        # =========================================================
        stage_width_list = [4, 8, 16, 32] * 4
        basemodel = ResNet_base(BasicBlock_baseV2, layer_list=[2, 2, 2, 2], stage_width_list=stage_width_list, num_classes=4)
        model = NetAugResNet18(base_net=basemodel, ResBlock=BasicBlock_baseV2, layer_list=[2, 2, 2, 2],
                               aug_width_mult_list=args.NetAug, n_classes=4)
        basemodel = model.export()
        # =========================================================
        # build Model
        # =========================================================
        # basemodel = MCUNet(num_classes=4, width=1)
        # model = NetAugMCUNet(base_net=basemodel, aug_width_mult_list=[1, 2], aug_expand_list=[1], n_classes=4)
        # model = model.to(device)
        model = model.to(device)
        basemodel = basemodel.to(device)
        #print(dataset.train_id)
        # ========================================================
        # build dataset
        #=========================================================
        dataset.train_id = train_ids
        train_dataset = data.Subset(dataset, train_ids)
        val_dataset = data.Subset(dataset, test_ids)
        test_dataset = data.Subset(dataset_test, test_ids)
        # ========================================================
        # build sampler
        #=========================================================
        targets = [dataset.labels[i] for i in train_ids]
        class_count = np.unique(targets, return_counts=True)[1]
        weight = 1. / class_count
        samples_weight = weight[targets]
        samples_weight = torch.from_numpy(samples_weight)
        sampler = WeightedRandomSampler(samples_weight, len(samples_weight))
        # ========================================================
        # build dataloader train for training val for validation(with Aug) test for testing(without Aug)
        #=========================================================
        kwargs = {'num_workers': 12, 'pin_memory': True} if device == 'cuda 1' else {}
        train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=args.batch_size, sampler=sampler, **kwargs)
        val_loader = torch.utils.data.DataLoader(val_dataset, batch_size=args.batch_size, **kwargs)
        test_loader = torch.utils.data.DataLoader(test_dataset, batch_size=args.batch_size, **kwargs)
        # ========================================================
        # build optimizer & scheduler
        #=========================================================
        params_without_wd = []
        params_with_wd = []
        for name, param in model.named_parameters():
            if param.requires_grad:
                if np.any([key in name for key in ["bias", "norm"]]):
                    print('with out wd', name)
                    params_without_wd.append(param)
                else:
                    print('with wd', name)
                    params_with_wd.append(param)
        net_params = [
            {"params": params_without_wd, "weight_decay": 0},
            {
                "params": params_with_wd,
                "weight_decay": 4.0e-5,
            },
        ]
        # ========================================================
        # build params 2
        #=========================================================
        # params_with_wd = []
        # for name, param in model.named_parameters():
        #     if param.requires_grad:
        #         params_with_wd.append(param)
        # net_params = [
        #     {"params": params_with_wd, "weight_decay": 0.1},
        # ]
        # optimizer = torch.optim.SGD(
        #     net_params,
        #     lr=args.lr,
        #     momentum=0.9,
        #     nesterov=True,
        # )
        optimizer = optim.AdamW(net_params, lr=args.lr)
        optimizer_base = optim.AdamW(basemodel.parameters(), lr=args.lr)
        #　scheduler = ReduceLROnPlateau(optimizer, mode='min', patience=7, verbose=True)
        # lr_scheduler = CosineLRwithWarmup(
        #     optimizer,
        #     warmup_steps=5 * len(train_loader),
        #     warmup_lr=args.lr,
        #     decay_steps=args.num_epochs * len(train_loader),
        # )
        lr_scheduler = CosineLRwithWarmup(
            optimizer,
            warmup_steps=0,
            warmup_lr=args.lr,
            decay_steps=args.num_epochs * len(train_loader),
        )
        lr_scheduler_base = CosineLRwithWarmup(
            optimizer_base,
            warmup_steps=0,
            warmup_lr=args.lr,
            decay_steps=args.num_epochs * len(train_loader),
        )
        # =========================================================
        # Initial Save Path
        # =========================================================
        save_path = os.path.join('trained_models_test', str(experiment_name))
        save_pth = os.path.join('pth_model', str(experiment_name))
        # entire_model_path = os.path.join('save_models', str(experiment_name))
        if not os.path.exists(save_path):
            os.makedirs(save_path)
        if not os.path.exists(save_pth):
            os.makedirs(save_pth)
        # =========================================================
        # Initial Weight&Bias
        # =========================================================
        wandb_logger = wandb.init(project=args.project_name,
                                  name=experiment_name,
                                  dir=save_path,
                                  )
        wandb.watch(models=(model), log_graph=True)
        # =========================================================
        # Training
        # =========================================================
        best_f1 = 0
        for epoch in range(args.num_epochs):
            train(model, basemodel, aug, device, train_loader, optimizer, optimizer_base, lr_scheduler, lr_scheduler_base, epoch, fold=fold, wandb_logger=wandb_logger)
            acc, results = test(model, basemodel, aug, device, val_loader, fold, result, epoch,
                                wandb_logger=wandb_logger)  # evaluate at the end of epoch

            val_f1 = calc_NetAug(model, device, test_loader, num_class)
            # test_f1 = calc(model, device, test_loader, num_class)
            averf1 = val_f1
            # scheduler.step(acc)
            if wandb_logger != None:
                log_stats = {"Average f1 {}".format(fold): averf1}
                wandb_logger.log(log_stats)


            model_save_path_1x = os.path.join(str(save_path), 'check_point_' + str(fold) + '.pth')
            model_save_path_max = os.path.join(str(save_path), 'check_point_max_' + str(fold) + '.pth')


            if averf1 > best_f1:

                print('best f1:' + str(averf1))
                print("save best model")
                # =========================================================
                # export max model
                # =========================================================
                #model.set_active('max')
                export_model = model.export()
                state_dict = {'model': export_model.state_dict(), 'optimizer': optimizer.state_dict(), 'epoch': epoch}
                torch.save(state_dict, model_save_path_max)
                # =========================================================
                # export min model
                # =========================================================
                #model.set_active('min')
                export_model = model.export()
                state_dict = {'model': export_model.state_dict(), 'optimizer': optimizer.state_dict(), 'epoch': epoch}
                torch.save(state_dict, model_save_path_1x)
                best_model_save_path_1x = model_save_path_1x
                best_f1 = averf1
        # =========================================================
        # EVAL the final results of Fold
        # =========================================================
        y_test, y_score = statics(basemodel, device, test_loader, best_model_save_path_1x, experiment_name, num_class)
        # =========================================================
        # Draw ------------------------
        # =========================================================
        path_txt = 'result/' + experiment_name + '/output.txt'
        path_txt2 = 'result/' + experiment_name + '/outputall.txt'
        path_f1 = 'result/' + experiment_name + '/f1.txt'
        with open(path_txt, 'a') as f:
            print('------------------------------', file=f)
        with open(path_txt2, 'a') as d:
            print('------------------------------', file=d)
        with open(path_f1, 'a') as k:
            print(str(best_f1), file=k)


if __name__ == '__main__':
    args = parse_option()
    main(args)
