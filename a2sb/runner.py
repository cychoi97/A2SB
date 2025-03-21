# ---------------------------------------------------------------
# Copyright (c) 2023, NVIDIA CORPORATION. All rights reserved.
#
# This work is licensed under the NVIDIA Source Code License
# for I2SB. To view a copy of this license, see the LICENSE file.
# ---------------------------------------------------------------

import os
import numpy as np
import pickle

import torch
import torch.nn.functional as F
from torch.optim import AdamW, lr_scheduler
from torch.nn.parallel import DistributedDataParallel as DDP

from torch_ema import ExponentialMovingAverage
import torchvision.utils as tu
# import torchmetrics

import distributed_util as dist_util
# from evaluation import build_resnet50

from . import util
from .network import Image256Net, Image512Net
from .diffusion import Diffusion

from ipdb import set_trace as debug

def build_optimizer_sched(opt, net, log):
    
    optim_dict = {"lr": opt.lr, 'weight_decay': opt.l2_norm}
    optimizer = AdamW(net.parameters(), **optim_dict)
    log.info(f"[Opt] Built AdamW optimizer {optim_dict}!")

    if opt.lr_gamma < 1.0:
        sched_dict = {"step_size": opt.lr_step, 'gamma': opt.lr_gamma}
        sched = lr_scheduler.StepLR(optimizer, **sched_dict)
        log.info(f"[Opt] Built lr step scheduler {sched_dict}!")
    else:
        sched = None

    if opt.load:
        checkpoint = torch.load(opt.load, map_location="cpu")
        if "optimizer" in checkpoint.keys():
            optimizer.load_state_dict(checkpoint["optimizer"])
            log.info(f"[Opt] Loaded optimizer ckpt {opt.load}!")
        else:
            log.warning(f"[Opt] Ckpt {opt.load} has no optimizer!")
        if sched is not None and "sched" in checkpoint.keys() and checkpoint["sched"] is not None:
            sched.load_state_dict(checkpoint["sched"])
            log.info(f"[Opt] Loaded lr sched ckpt {opt.load}!")
        else:
            log.warning(f"[Opt] Ckpt {opt.load} has no lr sched!")

    return optimizer, sched

def make_beta_schedule(n_timestep=1000, linear_start=1e-4, linear_end=2e-2):
    # return np.linspace(linear_start, linear_end, n_timestep)
    betas = (
        torch.linspace(linear_start ** 0.5, linear_end ** 0.5, n_timestep, dtype=torch.float64) ** 2
    )
    return betas.numpy()

def all_cat_cpu(opt, log, t):
    if not opt.distributed: return t.detach().cpu()
    gathered_t = dist_util.all_gather(t.to(opt.device), log=log)
    return torch.cat(gathered_t).detach().cpu()

class Runner(object):
    def __init__(self, opt, log, save_opt=True):
        super(Runner,self).__init__()

        # Save opt.
        if save_opt:
            opt_pkl_path = opt.ckpt_path / "options.pkl"
            with open(opt_pkl_path, "wb") as f:
                pickle.dump(opt, f)
            log.info("Saved options pickle to {}!".format(opt_pkl_path))

        betas = make_beta_schedule(n_timestep=opt.interval, linear_end=opt.beta_max / opt.interval)
        betas = np.concatenate([betas[:opt.interval//2], np.flip(betas[:opt.interval//2])])
        self.diffusion = Diffusion(betas, opt.device)
        log.info(f"[Diffusion] Built A2SB diffusion: steps={len(betas)}!")

        noise_levels = torch.linspace(opt.t0, opt.T, opt.interval, device=opt.device) * opt.interval
        if opt.image_size == 256:
            self.net = Image256Net(log, noise_levels=noise_levels,
                                   image_size=opt.image_size,
                                   in_channels=opt.in_channels,
                                   use_fp16=opt.use_fp16,
                                   cond=opt.cond_x1)
        elif opt.image_size == 512:
            self.net = Image512Net(log, noise_levels=noise_levels,
                                   image_size=opt.image_size,
                                   in_channels=opt.in_channels,
                                   use_fp16=opt.use_fp16,
                                   cond=opt.cond_x1)

        self.ema = ExponentialMovingAverage(self.net.parameters(), decay=opt.ema)

        if opt.load:
            checkpoint = torch.load(opt.load, map_location="cpu")
            self.net.load_state_dict(checkpoint['net'])
            log.info(f"[Net] Loaded network ckpt: {opt.load}!")
            self.ema.load_state_dict(checkpoint["ema"])
            log.info(f"[Ema] Loaded ema ckpt: {opt.load}!")

        self.net.to(opt.device)
        self.ema.to(opt.device)

        self.log = log

    def compute_label(self, step, x0, xt):
        """ Eq 12 """
        std_fwd = self.diffusion.get_std_fwd(step, xdim=x0.shape[1:])
        label = (xt - x0) / std_fwd
        return label.detach()

    def compute_pred_x0(self, step, xt, net_out, clip_denoise=False):
        """ Given network output, recover x0. This should be the inverse of Eq 12 """
        std_fwd = self.diffusion.get_std_fwd(step, xdim=xt.shape[1:])
        pred_x0 = xt - std_fwd * net_out
        if clip_denoise: pred_x0.clamp_(-1., 1.)
        return pred_x0

    def sample_batch(self, opt, loader):
        clean_img, corrupt_img, _ = next(loader)

        # os.makedirs(".debug", exist_ok=True)
        # tu.save_image((clean_img+1)/2, ".debug/clean.png", nrow=4)
        # tu.save_image((corrupt_img+1)/2, ".debug/corrupt.png", nrow=4)
        # debug()

        x0 = clean_img.detach().to(opt.device)
        x1 = corrupt_img.detach().to(opt.device)
        cond = x1.detach() if opt.cond_x1 else None

        assert x0.shape == x1.shape

        return x0, x1, cond

    def train(self, opt, train_dataset, val_dataset):
        self.writer = util.build_log_writer(opt)
        log = self.log

        net = DDP(self.net, device_ids=[opt.device], find_unused_parameters=True)
        ema = self.ema
        optimizer, sched = build_optimizer_sched(opt, net, log)

        train_loader = util.setup_loader(train_dataset, opt.microbatch)
        val_loader   = util.setup_loader(val_dataset,   opt.microbatch)

        net.train()

        n_inner_loop = opt.batch_size // (opt.global_size * opt.microbatch)
        for it in range(opt.start_itr, opt.num_itr + 1):
            optimizer.zero_grad()

            for _ in range(n_inner_loop):
                # ===== sample boundary pair =====
                x0, x1, cond = self.sample_batch(opt, train_loader)

                # ===== compute loss =====
                step = torch.randint(0, opt.interval, (x0.shape[0],))

                xt = self.diffusion.q_sample(step, x0, x1, ot_ode=opt.ot_ode)
                label = self.compute_label(step, x0, xt)

                pred, style_emb = net(x1, xt, step, cond=cond)
                assert x0.shape == xt.shape == label.shape == pred.shape

                loss = F.mse_loss(pred, label) + opt.lambda_reg * torch.mean(style_emb ** 2)
                loss.backward()

            optimizer.step()
            ema.update()
            if sched is not None: sched.step()

            # -------- logging --------
            log.info("train_it {}/{} | lr:{} | loss:{}".format(
                it,
                opt.num_itr,
                "{:.2e}".format(optimizer.param_groups[0]['lr']),
                "{:+.4f}".format(loss.item()),
            ))
            if it % 10 == 0:
                self.writer.add_scalar(it, 'loss', loss.detach())

            if it % 5000 == 0 or it == opt.num_itr:
                if opt.global_rank == 0:
                    torch.save({
                        "net": self.net.state_dict(),
                        "ema": ema.state_dict(),
                        "optimizer": optimizer.state_dict(),
                        "sched": sched.state_dict() if sched is not None else sched,
                    }, opt.ckpt_path / f"{it:07}.pt")
                    log.info(f"Saved latest(it={it}) checkpoint to {opt.ckpt_path}!")
                if opt.distributed:
                    torch.distributed.barrier()

            if it == 500 or it % 5000 == 0: # 0, 0.5k, 5k, 10k, 15k
                net.eval()
                self.evaluation(opt, it, val_loader, opt.ckpt_path) # mask delete
                net.train()
        self.writer.close()

    @torch.no_grad()
    def ddpm_sampling(self, opt, x0, x1, cond=None, generated_style=None, clip_denoise=False, nfe=None, log_count=10, verbose=True):

        # create discrete time steps that split [0, INTERVAL] into NFE sub-intervals.
        # e.g., if NFE=2 & INTERVAL=1000, then STEPS=[0, 500, 999] and 2 network
        # evaluations will be invoked, first from 999 to 500, then from 500 to 0.
        nfe = nfe or opt.interval-1
        assert 0 < nfe < opt.interval == len(self.diffusion.betas)
        steps = util.space_indices(opt.interval, nfe+1)

        # create log steps
        log_count = min(len(steps)-1, log_count)
        log_steps = [steps[i] for i in util.space_indices(len(steps)-1, log_count)]
        assert log_steps[0] == 0
        self.log.info(f"[DDPM Sampling] steps={opt.interval}, nfe={nfe}, log steps={log_steps}!")

        x0, x1 = x0.to(opt.device), x1.to(opt.device)
        if cond is not None: cond = cond.to(opt.device)
        if generated_style is not None: generated_style = generated_style.to(opt.device)

        with self.ema.average_parameters():
            self.net.eval()

            def pred_x0_fn(xt, step):
                step = torch.full((xt.shape[0],), step, device=opt.device, dtype=torch.long)
                out, _ = self.net(x1, xt, step, cond=cond, generated_style=generated_style)
                return self.compute_pred_x0(step, xt, out, clip_denoise=clip_denoise)

            xs, pred_x0 = self.diffusion.ddpm_sampling(
                steps, pred_x0_fn, x1, ot_ode=opt.ot_ode, log_steps=log_steps, verbose=verbose,
            )

        b, *xdim = x1.shape
        assert xs.shape == pred_x0.shape == (b, log_count, *xdim)

        return xs, pred_x0

    @torch.no_grad()
    def evaluation(self, opt, it, val_loader, ckpt_path):
        image_path = ckpt_path / 'valid_images'
        os.makedirs(image_path, exist_ok=True)

        log = self.log
        log.info(f"========== Evaluation started: iter={it} ==========")

        img_clean, img_corrupt, cond = self.sample_batch(opt, val_loader)

        x0, x1 = img_clean.to(opt.device), img_corrupt.to(opt.device)

        xs, pred_x0s = self.ddpm_sampling(
            opt, x0, x1, cond=cond, generated_style=None, clip_denoise=opt.clip_denoise, nfe=50, verbose=opt.global_rank==0
        )

        log.info("Collecting tensors ...")
        img_clean   = all_cat_cpu(opt, log, img_clean)
        img_corrupt = all_cat_cpu(opt, log, img_corrupt)
        pred_x0s    = all_cat_cpu(opt, log, pred_x0s)
        xs          = all_cat_cpu(opt, log, xs)

        batch, len_t, *xdim = xs.shape
        assert img_clean.shape == img_corrupt.shape == (batch, *xdim)
        assert xs.shape == pred_x0s.shape
        log.info(f"Generated recon trajectories: size={xs.shape}")

        def log_image(tag, img, nrow=10):
            self.writer.add_image(it, tag, tu.make_grid((img+1)/2, nrow=nrow)) # [1,1] -> [0,1]

        def log_metric(tag, score):
            self.writer.add_scalar(it, tag, score)

        log.info("Logging images ...")
        img_recon = xs[:, 0, ...]
        if opt.clip_denoise: img_recon.clamp_(-1., 1.)
        log_image("image/clean",   img_clean)
        log_image("image/corrupt", img_corrupt)
        log_image("image/recon",   img_recon)
        log_image("debug/pred_clean_traj", pred_x0s.reshape(-1, *xdim), nrow=len_t)
        log_image("debug/recon_traj",      xs.reshape(-1, *xdim),       nrow=len_t)

        # temp img png save
        tu.save_image(tu.make_grid((img_clean+1)/2, nrow=10, padding=0), os.path.join(image_path, f'{it:07}_target.png'), value_range=(0, 1))
        tu.save_image(tu.make_grid((img_corrupt+1)/2, nrow=10, padding=0), os.path.join(image_path, f'{it:07}_source.png'), value_range=(0, 1))
        tu.save_image(tu.make_grid((img_recon+1)/2, nrow=10, padding=0), os.path.join(image_path, f'{it:07}_target_recon.png'), value_range=(0, 1))
        tu.save_image(tu.make_grid((pred_x0s.reshape(-1, *xdim)+1)/2, nrow=len_t, padding=0), os.path.join(image_path, f'{it:07}_pred_target_traj.png'), value_range=(0, 1))
        tu.save_image(tu.make_grid((xs.reshape(-1, *xdim)+1)/2, nrow=len_t, padding=0), os.path.join(image_path, f'{it:07}_target_recon_traj.png'), value_range=(0, 1))

        from skimage.metrics import mean_squared_error as mse, peak_signal_noise_ratio as psnr, structural_similarity as ssim

        rmse_list = []
        psnr_list = []
        ssim_list = []

        for i in range(img_clean.size(0)):
            rmse_list.append(np.sqrt(mse((np.array(img_clean[i, 0, ...]+1)/2*255), np.array((img_recon[i, 0, ...]+1)/2*255))))
            psnr_list.append(psnr((np.array(img_clean[i, 0, ...]+1)/2*255), np.array((img_recon[i, 0, ...]+1)/2*255), data_range=256))
            ssim_list.append(ssim((np.array(img_clean[i, 0, ...]+1)/2*255), np.array((img_recon[i, 0, ...]+1)/2*255), data_range=256))
        
        log.info("Logging metrics ...")
        log.info(f"RMSE | Mean: {np.mean(rmse_list)}, SD: {np.std(rmse_list)}")
        log.info(f"PSNR | Mean: {np.mean(psnr_list)}, SD: {np.std(psnr_list)}")
        log.info(f"SSIM | Mean: {np.mean(ssim_list)}, SD: {np.std(ssim_list)}")
        
        log_metric("metric/Mean_RMSE", np.mean(rmse_list))
        log_metric("metric/Mean_PSNR", np.mean(psnr_list))
        log_metric("metric/Mean_SSIM", np.mean(ssim_list))

        log.info(f"========== Evaluation finished: iter={it} ==========")
        torch.cuda.empty_cache()
