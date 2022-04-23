import sys
import argparse
import random
from pathlib import Path
import os
from types import SimpleNamespace
from urllib.error import HTTPError
import pandas as pd
import numpy as np
from skimage import io
import pytorch_lightning as pl
from pytorch_lightning.lite import LightningLite
from pytorch_lightning.loops import Loop
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset
import torchvision
import torchvision.models as models
import torch.nn.functional as F
from sklearn.preprocessing import LabelEncoder
from sklearn.metrics import roc_auc_score
from tqdm import tqdm
from typing import Callable, Union, Optional, Any

# from IPython.display import HTML, display, set_matplotlib_formats
from PIL import Image
from pytorch_lightning.callbacks import LearningRateMonitor, ModelCheckpoint, EarlyStopping
from torchvision import transforms


class MILdataset(Dataset):
    def __init__(self, libraryfile_dir='', root_dir='', dataset_mode='Train', transform=None, subset_rate=None):
        libraryfile_path = os.path.join(
            libraryfile_dir, f'CRC_DX_{dataset_mode}_ALL.csv')
        lib = pd.read_csv(libraryfile_path)
        lib = lib if subset_rate is None else lib.sample(
            frac=subset_rate, random_state=2022)
        lib = lib.sort_values(['subject_id'], ignore_index=True)
        lib.to_csv(os.path.join(libraryfile_dir,
                   f'{dataset_mode}_temporary.csv'))
        slides = []
        for i, name in enumerate(lib['subject_id'].unique()):
            sys.stdout.write(
                'Slides: [{}/{}]\r'.format(i+1, len(lib['subject_id'].unique())))
            sys.stdout.flush()
            slides.append(name)

        # Flatten grid
        grid = []
        slideIDX = []
        for i, g in enumerate(lib['subject_id'].unique()):
            tiles = lib[lib['subject_id'] == g]['slice_id']
            grid.extend(tiles)
            slideIDX.extend([i]*len(tiles))

        # print('Number of tiles: {}'.format(len(grid)))
        self.dataframe = self.load_data_and_get_class(lib)
        self.slidenames = list(lib['subject_id'].values)
        self.slides = slides
        self.targets = list(lib['Class'].values)
        self.grid = grid
        self.slideIDX = slideIDX
        self.transform = transform
        self.root_dir = root_dir
        self.dset = f"CRC_DX_{dataset_mode}"

    def setmode(self, mode):
        self.mode = mode

    def maketraindata(self, idxs):
        self.t_data = [(self.slideIDX[x], self.grid[x],
                        self.targets[x]) for x in idxs]

    def shuffletraindata(self):
        self.t_data = random.sample(self.t_data, len(self.t_data))

    def load_data_and_get_class(self, df):
        df.loc[df['label']=='MSI', 'Class'] = 1
        df.loc[df['label']=='MSS', 'Class'] = 0
        return df

    def __getitem__(self, index):
        if self.mode == 1:
            slideIDX = self.slideIDX[index]
            tile_id = self.grid[index]
            slide_id = self.slides[slideIDX]
            img_name = "blk-{}-{}.png".format(tile_id, slide_id)
            target = self.dataframe.loc[index, 'Class']
            label = 'CRC_DX_MSIMUT' if target == 0 else 'CRC_DX_MSS'
            img_path = os.path.join(self.root_dir, self.dset, label, img_name)
            img = io.imread(img_path)
            if self.transform is not None:
                img = self.transform(img)
            return img
        elif self.mode == 2:
            slideIDX, tile_id, target = self.t_data[index]
            slide_id = self.slides[slideIDX]
            label = 'CRC_DX_MSIMUT' if target == 0 else 'CRC_DX_MSS'
            img_name = "blk-{}-{}.png".format(tile_id, slide_id)
            img_path = os.path.join(self.root_dir, self.dset, label, img_name)
            img = io.imread(img_path)

        if self.transform is not None:
            img = self.transform(img)
        return img, target

    def __len__(self):
        if self.mode == 1:
            return len(self.grid)
        elif self.mode == 2:
            return len(self.t_data)

class MIL_Module(pl.LightningModule):
    def __init__(
        self, model_name, model_hparams, optimizer_name, optimizer_hparams, args, train_dataset, val_dataset):
        super().__init__()
        self.args = args
        self.save_hyperparameters()
        self.model = models.resnet18(pretrained=True)
        self.loss_module =  nn.CrossEntropyLoss(torch.Tensor([1-args.weights, args.weights]))
        self.train_dataset = train_dataset
        self.val_dataset = val_dataset
        self.train_dataloader = DataLoader(self.train_dataset, batch_size=self.args.batch_size, shuffle=False, num_workers=self.args.num_workers, pin_memory=True)
        self.val_dataloader = DataLoader(self.val_dataset, batch_size=self.args.batch_size, shuffle=False, num_workers=self.args.num_workers, pin_memory=True)
    
    
    def forward(self, x):
        return self.model(x)

    def configure_optimizers(self):
        if self.hparams.optimizer_name[0] == "Adam":
            optimizer = torch.optim.AdamW(self.parameters(),**self.hparams.optimizer_hparams[0])
        elif self.hparams.optimizer_name[0] == "SGD":
            optimizer = torch.optim.SGD(self.parameters(), **self.hparams.optimizer_hparams[0])
        else:
            assert False, f'Unknown optimizer: "{self.hparams.optimizer_name}"'
        # Reduce the learning rate by 0.1 after 50 and 100 epochs
        scheduler = torch.optim.lr_scheduler.MultiStepLR(
            optimizer, milestones=[50, 100], gamma=0.1)
        return [optimizer], [scheduler]

    def on_train_epoch_start(self):
        self.train_dataset.setmode(1)
        probs = self.predict( self.model, dataloaders=self.train_dataloader)
        # return the indices of topk tile(s) in each slides
        topk = self.group_argtopk(np.array(self.train_dataset.slideIDX), probs, self.args.k)
        self.train_dataset.maketraindata(topk)
        self.train_dataset.shuffletraindata()
        self.train_dataset.setmode(2)

    def training_step(self, batch, batch_idx):
        input, labels = batch
        output = self.model(input)
        loss = self.loss_module(output, labels)
        acc = (output.argmax(dim=-1) == labels).float().mean()
        # Logs the accuracy per epoch to tensorboard (weighted average over batches)
        self.log("train_acc", acc, on_step=False, on_epoch=True)
        self.log("train_loss", loss, on_step=False, on_epoch=True)

    def on_val_epoch_start(self):
        self.val_dataset.setmode(1)

    def validation_step(self, batch, batch_idx):
        input, labels = batch
        probs = self.model(input)
        pred = [1 if x>= 0.5 else 0 for x in probs]
        acc, err, fpr, fnr = self.calc_err(pred, labels)
        auroc_score = roc_auc_score(labels, probs)
        # By default logs it per epoch (weighted average over batches)
        self.log("val_acc", acc)
        self.log("val_err", err)
        self.log("fpr", fpr)
        self.log("fnr", fnr)
        self.log("auroc_score", auroc_score)

    def predict_step(self, batch, batch_idx) -> Any:
        input, labels = batch
        probs = F.softmax(self.model(input), dim=1)
        return probs

    def inference(self, loader, model):
        model.eval()
        probs = torch.FloatTensor(len(loader.dataset))
        with torch.no_grad():
            for i, input in enumerate(loader):
                output = F.softmax(model(input), dim=1)
                probs[i*self.args.batch_size:i*self.args.batch_size + input.size(0)] = output.detach()[:, 1].clone()
        return probs.cpu().numpy()

    def calc_err(pred, real):
        pred = np.array(pred)
        real = np.array(real)
        pos = np.equal(pred, real)
        neq = np.not_equal(pred, real)
        acc = float(pos.sum())/pred.shape[0]
        err = float(neq.sum())/pred.shape[0]
        fpr = float(np.logical_and(pred == 1, neq).sum())/(real == 0).sum()
        fnr = float(np.logical_and(pred == 0, neq).sum())/(real == 1).sum()
        return acc, err, fpr, fnr

    def group_argtopk(groups, data, k=1):
        # groups in slide, data is prob of each tile
        order = np.lexsort((data, groups))
        groups = groups[order]
        data = data[order]
        index = np.empty(len(groups), 'bool')
        index[-k:] = True
        index[:-k] = groups[k:] != groups[:-k]
        return list(order[index])  # output top prob tile index in each slide

    def group_max(groups, data, nmax):
        out = np.empty(nmax)
        out[:] = np.nan
        order = np.lexsort((data, groups))
        groups = groups[order]
        data = data[order]
        index = np.empty(len(groups), 'bool')
        index[-1] = True
        index[:-1] = groups[1:] != groups[:-1]
        out[groups[index]] = data[index]
        return out

class MIL_DataModule(pl.LightningDataModule):
    def __init__(
        self,
        data_path: str,
        train_transform: Callable,
        val_transform: Callable,
        test_transform: Callable,
        batch_size: int = 1,
        num_workers: int = 1,
    ):

        super().__init__()
        self.data_path = data_path
        self.train_transform = train_transform
        self.val_transform = val_transform
        self.test_transform = test_transform
        self.batch_size = batch_size
        self.num_workers = num_workers

    def train_dataloader(self):
        
        train_DataLoader = DataLoader(train_dataset, batch_size=self.batch_size, shuffle=True,
                                      num_workers=self.num_workers, pin_memory=True)
        return train_DataLoader

    def val_dataloader(self):
        train_path = os.path.join(self.data_path, "CRC_DX_Val")
        val_dataset = torchvision.datasets.ImageFolder(train_path, self.val_transform)
        val_DataLoader = DataLoader(val_dataset, batch_size=self.batch_size, shuffle=False,
                                    num_workers=self.num_workers, pin_memory=True)
        return val_DataLoader

    def test_dataloader(self):
        train_path = os.path.join(self.data_path, "CRC_DX_Test")
        test_dataset = torchvision.datasets.ImageFolder(train_path, self.test_transform)
        test_DataLoader = DataLoader(test_dataset, batch_size=self.batch_size, shuffle=False,
                                     num_workers=self.num_workers, pin_memory=True)
        return test_DataLoader

def main(args):
    # args = args
    #Environment
    DATASET_PATH = os.environ.get("PATH_DATASETS", "data/")
    CHECKPOINT_PATH = os.environ.get("PATH_CHECKPOINT", "saved_models/ConvNets")
    pl.seed_everything(2022)

    torch.backends.cudnn.determinstic = True
    torch.backends.cudnn.benchmark = False

    device = torch.device("cuda:0") if torch.cuda.is_available() else torch.device("cpu")
    # data
    DATA_MEANS = [0.485, 0.456, 0.406]
    DATA_STD = [0.229, 0.224, 0.225]
    train_transform = transforms.Compose([transforms.RandomHorizontalFlip(),transforms.ToTensor(),transforms.Normalize(DATA_MEANS, DATA_STD)])
    test_transform = transforms.Compose([transforms.ToTensor(), transforms.Normalize(DATA_MEANS, DATA_STD)])
    train_dataset = MILdataset(
        args.lib_dir, args.root_dir, 'Train', transform=train_transform, subset_rate=0.001)
    val_dataset = MILdataset(
        args.lib_dir, args.root_dir, 'Val', transform=test_transform, subset_rate=0.001)
    test_dataset = MILdataset(
        args.lib_dir, args.root_dir, 'Test', transform=test_transform, subset_rate=0.001)

    # model
    model_name = "resnet18"
    model_hparams={"num_classes": 2, "act_fn_name": "relu"}
    optimizer_name="Adam",
    optimizer_hparams={"lr": 1e-3, "weight_decay": 1e-4},
    model = MIL_Module(model_name, model_hparams, optimizer_name, optimizer_hparams, args, train_dataset, val_dataset)

    # training
    trainer = pl.Trainer(
        default_root_dir=os.path.join(CHECKPOINT_PATH, model_name),
        gpus=1 if str(device) == "cuda:0" else 0,
        min_epochs=10,
        max_epochs=args.nepochs,
        callbacks=[ModelCheckpoint(save_weights_only=True, mode="max", monitor="val_acc"), 
        LearningRateMonitor("epoch")],
        auto_lr_find=True
    )
    trainer.logger._log_graph = True  # If True, we plot the computation graph in tensorboard
    trainer.logger._default_hp_metric = None  # Optional logging argument that we don't need

    # [Optional] lr_finder
    lr_finder = trainer.tuner.lr_find(model,train_dataloader=MIL_Module.train_dataloader,val_dataloaders=MIL_Module.val_dataloader)
    model.hparams.learning_rate = lr_finder.suggestion()

    # fit
    trainer.fit(model, train_dataloader=MIL_Module.train_dataloader,val_dataloaders=MIL_Module.val_dataloader)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "--root_dir",
        type=Path,
        required=True,
        help="root directory of dataset",
    )
    parser.add_argument(
        "--lib_dir",
        type=Path,
        required=True,
        help="root directory of libraryfile",
    )
    parser.add_argument(
        "--output_path",
        type=Path,
        required=True,
        help="output directory",
    )
    parser.add_argument(
        "--batch_size",
        default=128,
        type=int,
        help="batch size",
    )
    parser.add_argument(
        "--num_workers",
        default=0,
        type=int,
        required=True,
        help="number of workers",
    )
    parser.add_argument(
        "--nepochs",
        default=50,
        type=int,
        help="training epoch",
    )
    parser.add_argument(
        '--test_every',
        default=10,
        type=int,
        help='test on val every (default: 10)')

    parser.add_argument(
        "--weights",
        default=0.5,
        type=float,
        help="unbalanced positive class weight (default: 0.5, balanced classes)",
    )

    parser.add_argument(
        "--k",
        default=1,
        type=int,
        help="top k tiles are assumed to be of the same class as the slide (default: 1, standard MIL)",
    )
    # args = parser.parse_args()
    class Args:
        root_dir = '/Users/gaskell/Dropbox/Mac/Desktop/CBH/ex_data/CRC_DX_data_set/Dataset'
        lib_dir = '/Users/gaskell/Dropbox/Mac/Desktop/CBH/ex_data/CRC_DX_data_set/CRC_DX_Lib'
        output_path = '/Users/gaskell/Dropbox/Mac/Desktop/CBH/ex_data/CRC_DX_data_set/Output'
        batch_size = 128
        nepochs = 2
        num_workers = 1
        test_every = 1
        weights = 0.5
        k = 1

    args = Args()
    main(args)
