import os
# os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3" 
# import tensorflow as tf

import os.path as osp
import argparse
import time
import torch
from torch.utils.data import DataLoader
import numpy as np
from tqdm import tqdm
import yaml
from attrdict import AttrDict
import wandb
import pickle

from models.tnpd import TNPD
from models.tnpa import TNPA
from models.convlnp import CONVLNP
from clidow import ClimateDataset
# from data.data_utils import ClimateDataset

from utils.metrics import correlations, mae, mean_bias
from utils.paths import results_path, datasets_path, evalsets_path
from utils.misc import load_module
from utils.log import get_logger, RunningAverage

def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ("yes", "true", "t", "y", "1"):
        return True
    elif v.lower() in ("no", "false", "f", "n", "0"):
        return False
    else:
        raise argparse.ArgumentTypeError("Boolean value expected.")

def main():
    parser = argparse.ArgumentParser()

    # Experiment
    parser.add_argument("--mode", choices=["train", "eval"], default="train")
    parser.add_argument("--expid", type=str, default="default")
    parser.add_argument("--resume", type=str, default=None)
    parser.add_argument("--gpu", type=int, default=0)  # default(-1): device="cpu"
    parser.add_argument("--wandb", type=str2bool, nargs="?", const=True, default=False)

    # Data
    parser.add_argument("--data", type=str, default="data")
    parser.add_argument("--area", type=str, default="germany")
    parser.add_argument("--station_in_dim", type=int, default=3)

    # Model
    parser.add_argument("--model", type=str, default="tnpd")
    parser.add_argument("--config", type=str, required=True)

    # Train
    parser.add_argument("--multivariate", type=str2bool, nargs="?", const=True, default=True)
    parser.add_argument("--temporal_context", type=str2bool, nargs="?", const=True, default=False)
    parser.add_argument("--temporal_target", type=str2bool, nargs="?", const=True, default=False)
    parser.add_argument("--history_len", type=int, default=10)
    parser.add_argument("--offset", type=int, default=10)
    parser.add_argument("--overwrite", action="store_true", default = False)
    parser.add_argument("--variable", type=str, choices=["tmax", "prep"], default="tmax") # only matters when multivariate == False
    parser.add_argument("--train_seed", type=int, default=0)
    parser.add_argument("--train_batch_size", type=int, default=64)
    parser.add_argument("--lr", type=float, default=5e-4)
    parser.add_argument("--train_num_samples", type=int, default=64)
    parser.add_argument("--num_epochs", type=int, default=150)
    parser.add_argument("--eval_freq", type=int, default=10)
    parser.add_argument("--save_freq", type=int, default=10)

    # Eval
    parser.add_argument("--eval_seed", type=int, default=0)
    parser.add_argument("--eval_num_samples", type=int, default=64)
    parser.add_argument("--eval_batch_size", type=int, default=16)
    parser.add_argument("--eval_logfile", type=str, default=None)

    args = parser.parse_args()
    os.environ["CUDA_VISIBLE_DEVICES"] = str(args.gpu)

    args.root = osp.join(results_path, args.model, args.expid)

    model_cls = getattr(load_module(f"models/{args.model}.py"), args.model.upper())
    with open(args.config, "r") as f:
        config = yaml.safe_load(f)
        args.config = config
        config["multivariate"] = args.multivariate
        config["temporal_context"] = args.temporal_context
        config["temporal_target"] = args.temporal_target
        config["history_len"] = args.history_len
        if not args.multivariate:
            config["station_out_dim"] = 1
            config["variable"] = args.variable
    model = model_cls(**config)
    if torch.cuda.device_count() > 1:
        model = torch.nn.DataParallel(model)
    model.cuda()

    if args.mode == "train":
        train(args, model)
    elif args.mode == "eval":
        eval(args, model)

def prepare_data(low_res_data, station_data, args):
    low_res_data = low_res_data.float().cuda() # (batch, #hrs, 6, 7, #features)
    station_data = station_data.float().cuda()
    station_in = station_data[..., :args.station_in_dim]
    if args.multivariate:
        station_out = station_data[..., args.station_in_dim:]
    else:
        if args.variable == "tmax":
            station_out = station_data[..., args.station_in_dim]
        else:
            station_out = station_data[..., args.station_in_dim+1]
        station_out = station_out.unsqueeze(-1)
    return low_res_data, station_in, station_out

def train(args, model):
    if osp.exists(args.root + "/ckpt.tar"):
        if args.resume is None:
            if not args.overwrite:
                raise FileExistsError(args.root)
    else:
        os.makedirs(args.root, exist_ok=True)

    with open(osp.join(args.root, "args.yaml"), "w") as f:
        yaml.dump(args.__dict__, f)

    train_dataset = ClimateDataset(
        root=args.data,
        area=args.area,
        train=True,
        temporal_context=args.temporal_context,
        temporal_target=args.temporal_target,
        history_len=args.history_len,
        offset=args.offset
    )
    train_dataloader = DataLoader(train_dataset, batch_size=args.train_batch_size, shuffle=True)

    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=len(train_dataloader)*args.num_epochs)

    if args.resume:
        ckpt = torch.load(osp.join(args.root, "ckpt.tar"))
        model.load_state_dict(ckpt.model)
        optimizer.load_state_dict(ckpt.optimizer)
        scheduler.load_state_dict(ckpt.scheduler)
        logfilename = ckpt.logfilename
        start_epoch = ckpt.epoch
    else:
        logfilename = osp.join(args.root, "train_{}.log".format(
            time.strftime("%Y%m%d-%H%M")))
        start_epoch = 1

    logger = get_logger(logfilename)
    ravg = RunningAverage()

    if args.wandb:
        wandb.init(
            project="seqclimate", name=f"{args.model}-{args.expid}",
            entity="seqcnpclimate", config=args.__dict__
        )

    if not args.resume:
        logger.info("Total number of parameters: {}\n".format(
            sum(p.numel() for p in model.parameters())))

    for epoch in range(start_epoch, args.num_epochs+1):
        model.train()
        for (low_res_data, station_data) in tqdm(train_dataloader, ascii=True):
            low_res_data, station_in, station_out = \
                prepare_data(low_res_data, station_data, args)
  
            optimizer.zero_grad()
            
            if args.model in ["tnpa", "tnpd", "convcnp"]:
                outs = model(low_res_data, station_in, station_out)
            elif args.model in ["convlnp"]:
                outs = model(low_res_data, station_in, station_out, num_samples=args.train_num_samples)
            
            loss = - outs.tar_ll
            if torch.cuda.device_count() > 1:
                loss = torch.mean(loss)
                for k in outs.keys():
                    outs[k] = torch.mean(outs[k])
            loss.backward()
            
            
            optimizer.step()
            scheduler.step()

            for key, val in outs.items():
                ravg.update(key, val)
        
        line = f"{args.model}:{args.expid} epoch {epoch} "
        line += f"lr {optimizer.param_groups[0]['lr']:.3e} "
        line += ravg.info()
        logger.info(line)

        if args.wandb:
            wandb.log(ravg.to_dict(), step=epoch)
        ravg.reset()

        if epoch % args.eval_freq == 0:
            line, test_ravg = eval(args, model)
            logger.info(line + "\n")
            if args.wandb:
                wandb.log(test_ravg.to_dict())

        if epoch % args.save_freq == 0 or epoch == args.num_epochs:
            ckpt = AttrDict()
            ckpt.model = model.state_dict()
            ckpt.optimizer = optimizer.state_dict()
            ckpt.scheduler = scheduler.state_dict()
            ckpt.logfilename = logfilename
            ckpt.epoch = epoch + 1
            torch.save(ckpt, osp.join(args.root, "ckpt.tar"))
    
    args.mode = "eval"
    eval(args, model)
    
    if args.wandb:
        wandb.finish()

def eval(args, model):
    if args.mode == "eval":
        ckpt = torch.load(osp.join(args.root, "ckpt.tar"))
        model.load_state_dict(ckpt.model)
        if args.eval_logfile is None:
            eval_logfile = f"eval.log"
        else:
            eval_logfile = args.eval_logfile
        filename = osp.join(args.root, eval_logfile)
        logger = get_logger(filename, mode="w")
    else:
        logger = None

    eval_dataset = ClimateDataset(
        root=args.data,
        area=args.area,
        train=False,
        temporal_context=args.temporal_context,
        temporal_target=args.temporal_target,
        history_len=args.history_len,
        offset=args.offset
    )
    eval_dataloader = DataLoader(eval_dataset, batch_size=args.eval_batch_size, shuffle=False)

    torch.manual_seed(args.eval_seed)
    torch.cuda.manual_seed(args.eval_seed)

    ravg = RunningAverage()
    model.eval()
    with torch.no_grad():
        pred_stds = None
        for (low_res_data, station_data) in tqdm(eval_dataloader, ascii=True):
            low_res_data, station_in, station_out = \
                prepare_data(low_res_data, station_data, args)

            if args.model in ["tnpa", "tnpd"]:
                # pred_dist = model.predict(low_res_data, station_in, station_out)
                if torch.cuda.device_count() > 1:
                    pred_dist = model.module.predict(low_res_data, station_in, station_out)
                else:
                    pred_dist = model.predict(low_res_data, station_in, station_out)
            elif args.model in ["convcnp"]:
                pred_dist = model.predict(low_res_data, station_in)
            elif args.model in ["convlnp"]:
                pred_dist = model.predict(low_res_data, station_in, num_samples=args.eval_num_samples)
            pred_mean = pred_dist.mean
            pred_std = torch.sqrt(pred_dist.variance)
            if pred_mean.dim() == 4:
                pred_mean = pred_mean.mean(dim=0)
                pred_std = pred_std.mean(dim=0)
            if len(station_out.shape) == 4:
                station_out = station_out[:, -1]
                    
            pred_mean = pred_mean.detach().cpu().numpy()
            station_out = station_out.cpu().numpy()
            
            pred_std = pred_std.detach().cpu()
            pred_stds = torch.cat([pred_stds, pred_std], dim = 0) if(pred_stds is not None) else pred_std

            pred = {}
            truths = {}
            if args.multivariate or args.variable == "tmax":
                pred["tmax"] = pred_mean[:, :, 0].flatten()
                truths["tmax"] = station_out[:, :, 0].flatten()
            if args.multivariate or args.variable == "prep":
                pred["prep"] = pred_mean[:, :, -1].flatten()
                truths["prep"] = station_out[:, :, -1].flatten()

            outs = AttrDict()
            if "tmax" in pred.keys():
                outs.tmax_mae = mae(pred["tmax"], truths["tmax"])
                outs.tmax_mbs = mean_bias(pred["tmax"], truths["tmax"])
                outs.tmax_sp, outs.tmax_pr = correlations(pred["tmax"], truths["tmax"])
            if "prep" in pred.keys():
                outs.prep_mae = mae(pred["prep"], truths["prep"])
                outs.prep_mbs = mean_bias(pred["prep"], truths["prep"])
                outs.prep_sp, outs.prep_pr = correlations(pred["prep"], truths["prep"])

            for key, val in outs.items():
                ravg.update(key, val)
        
        if(args.multivariate or args.variable == "tmax"):
            pickle.dump(pred_stds[..., 0], open(os.path.join(args.root, "std.tmax.pkl"), "wb"))
            
        if(args.multivariate or args.variable == "prep"):
            pickle.dump(pred_stds[..., -1], open(os.path.join(args.root, "std.prep.pkl"), "wb"))

    torch.manual_seed(time.time())
    torch.cuda.manual_seed(time.time())

    line = f"{args.model}:{args.expid} "
    line += ravg.info()

    if logger is not None:
        logger.info(line)

    return line, ravg

if __name__ == "__main__":
    main()