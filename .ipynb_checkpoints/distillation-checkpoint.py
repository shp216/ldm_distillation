import torch
from omegaconf import OmegaConf
import numpy as np
from PIL import Image
from einops import rearrange
from torchvision.utils import make_grid

from ldm.util import instantiate_from_config
from ldm.models.diffusion.ddim import DDIMSampler
from absl import app, flags
import warnings

from omegaconf import OmegaConf
import random
import os
import math
import wandb
import random
import logging
import inspect
import argparse
import datetime
import subprocess
import warnings
import threading
import GPUtil
import diffusers
import argparse, os, sys, glob, yaml, math, random
from diffusers.optimization import get_scheduler
from diffusion import GaussianDiffusion_distillation_Trainer, GaussianDiffusionTrainer, GaussianDiffusionSampler, distillation_cache_Trainer, GaussianDiffusion_joint_Sampler
from pytorch_lightning import seed_everything
import torch.nn.init as init

from trainer import distillation_DDPM_trainer
from funcs import load_model_from_config, get_model_teacher, load_model_from_config_without_ckpt, get_model_student, initialize_params, sample_save_images, save_checkpoint
device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')


def get_parser():
    parser = argparse.ArgumentParser()
    
    parser.add_argument("--seed", type=int, default=20240911, help="seed for seed_everything")

    parser.add_argument("--learning_rate", type=float, default=3e-5, help="Learning rate for training")
    parser.add_argument("--scale_lr", type=bool, default=False, help="Flag to scale learning rate")
    parser.add_argument("--lr_warmup_steps", type=int, default=0, help="Number of learning rate warmup steps")
    parser.add_argument("--lr_scheduler", type=str, default="constant", help="Learning rate scheduler type")
    parser.add_argument("--gradient_accumulation_steps", type=int, default=1, help="Number of gradient accumulation steps")

    
    parser.add_argument("--trainable_modules", type=tuple, default=(None,), help="Tuple of trainable modules")
    parser.add_argument("--train_batch_size", type=int, default=4, help="Batch size for training")
    parser.add_argument("--adam_beta1", type=float, default=0.9, help="Beta1 parameter for Adam optimizer")
    parser.add_argument("--adam_beta2", type=float, default=0.999, help="Beta2 parameter for Adam optimizer")
    parser.add_argument("--adam_weight_decay", type=float, default=1e-2, help="Weight decay for Adam optimizer")
    parser.add_argument("--adam_epsilon", type=float, default=1e-08, help="Epsilon parameter for Adam optimizer")

    # Gaussian Diffusion
    parser.add_argument("--beta_1", type=float, default=1e-4, help='start beta value')
    parser.add_argument("--beta_T", type=float, default=0.02, help='end beta value')
    parser.add_argument("--T", type=int, default=1000, help='total diffusion steps')
    parser.add_argument("--mean_type", type=str, choices=['xprev', 'xstart', 'epsilon'], default='epsilon', help='predict variable')
    parser.add_argument("--var_type", type=str, choices=['fixedlarge', 'fixedsmall'], default='fixedlarge', help='variance type')
    
    # Training
    parser.add_argument("--lr", type=float, default=1e-5, help='target learning rate')
    parser.add_argument("--grad_clip", type=float, default=1., help="gradient norm clipping")
    parser.add_argument("--total_steps", type=int, default=800000, help='total training steps')
    parser.add_argument("--img_size", type=int, default=32, help='image size')
    parser.add_argument("--warmup", type=int, default=5000, help='learning rate warmup')
    parser.add_argument("--batch_size", type=int, default=128, help='batch size')
    parser.add_argument("--num_workers", type=int, default=4, help='workers of Dataloader')
    parser.add_argument("--ema_decay", type=float, default=0.9999, help="ema decay rate")
    parser.add_argument("--parallel", action='store_true', help='multi gpu training')
    parser.add_argument("--distill_features", action='store_true', default=True, help='perform knowledge distillation using intermediate features')
    
    # Logging & Sampling
    parser.add_argument("--logdir", type=str, default='./logs/DDPM_CIFAR10_EPS', help='log directory')
    parser.add_argument("--sample_size", type=int, default=32, help="sampling size of images")
    parser.add_argument("--sample_step", type=int, default=10000, help='frequency of sampling')
    
    # WandB 관련 FLAGS 추가
    parser.add_argument("--wandb_project", type=str, default='distill_caching_ddpm', help='WandB project name')
    parser.add_argument("--wandb_run_name", type=str, default=None, help='WandB run name')
    parser.add_argument("--wandb_notes", type=str, default='', help='Notes for the WandB run')
    
    # Evaluation
    parser.add_argument("--save_step", type=int, default=50000, help='frequency of saving checkpoints, 0 to disable during training')
    parser.add_argument("--eval_step", type=int, default=100000, help='frequency of evaluating model, 0 to disable during training')
    parser.add_argument("--num_images", type=int, default=50000, help='the number of generated images for evaluation')
    parser.add_argument("--fid_use_torch", action='store_true', help='calculate IS and FID on gpu')
    parser.add_argument("--fid_cache", type=str, default='./stats/cifar10.train.npz', help='FID cache')
    
    # Caching
    parser.add_argument("--cache_n", type=int, default=64, help='size of caching data per timestep')
    parser.add_argument('--cachedir', type=str, default='./cache', help='log directory')
    parser.add_argument("--is_precache", action="store_true", help="whether to perform pre-caching")



    #DDIM Sampling
    parser.add_argument("--DDIM_num_steps", type=int, default=50, help='number of DDIM samping steps')

    parser.add_argument("--num_sample_class", type=int, default=4, help='number of class for save and sampling')
    parser.add_argument("--n_sample_per_class", type=int, default=16, help='number of sample for per class in save_sample')

    parser.add_argument("--sample_save_ddim_steps", type=int, default=20, help='number of DDIM sampling steps')
    parser.add_argument("--ddim_eta", type=float, default=1.0, help='DDIM eta parameter for noise level')
    parser.add_argument("--scale", type=float, default=1.5, help='guidance scale for unconditional guidance')

    #Directory
    parser.add_argument('--logdir', type=str, default='./logs', help='log directory')


    
    return parser

def distillation(args):

    # Initialize WandB
    wandb.init(
        project=args.wandb_project,
        name=args.wandb_run_name,
        notes=args.wandb_notes,
        config={
            "learning_rate": args.lr,
            "architecture": "UNet",
            "dataset": "your_dataset_name",
            "epochs": args.total_steps,
        }
    )
    
    T_model = get_model_teacher()
    S_model= get_model_student()
    initialize_params(student_model)

    all_params_student = list(S_model.parameters())
    trainable_params_student = list(filter(lambda p: p.requires_grad, S_model.parameters()))
    
    all_params_teacher = list(T_model.parameters())
    trainable_params_teacher = list(filter(lambda p: p.requires_grad, T_model.parameters()))
    
    num_all_params_student= sum(p.numel() for p in all_params_student)
    num_trainable_params_student = sum(p.numel() for p in trainable_params_student)

    num_trainable_params_teacher = sum(p.numel() for p in trainable_params_teacher)
    num_all_params_teacher = sum(p.numel() for p in all_params_teacher)
    
    print(f"Student: Number of All parameters: {num_all_params_student}")
    print(f"Student: Number of trainable parameters: {num_trainable_params_student}")

    print(f"Teacher: Number of All parameters: {num_all_params_teacher}")
    print(f"Teacher: Number of trainable parameters: {num_trainable_params_teacher}")

    optimizer = torch.optim.AdamW(
        trainable_params_student,
        lr=args.learning_rate,
        betas=(args.adam_beta1, args.adam_beta2),
        weight_decay=args.adam_weight_decay,
        eps=args.adam_epsilon,
    )

    lr_scheduler = get_scheduler(
        args.lr_scheduler,
        optimizer=optimizer,
        num_warmup_steps=args.lr_warmup_steps * args.gradient_accumulation_steps,
        num_training_steps=args.total_steps
    )

    T_sampler = DDIMSampler(T_model)
    S_sampler = DDIMSampler(S_model)
    
    T_sampler.make_schedule(ddim_num_steps = args.DDIM_num_steps, ddim_eta= 1, verbose=False)
    S_sampler.make_schedule(ddim_num_steps = args.DDIM_num_steps, ddim_eta= 1, verbose=False)
     
    trainer = distillation_DDPM_trainer(
        T_model, S_model, T_sampler, S_sampler, args.train_is_feature, args.beta_1, args.beta_T, args.T,
        args.mean_type, args.var_type, args.distill_features).to(device)

    if args.is_precache:
        ############################################ precacheing ##################################################
        cache_size = args.cache_n*1000
        
        img_cache = torch.randn(cache_size, T_model.channels, T_model.img_size, T_model.img_size).to(device)
        t_cache = torch.ones(cache_size, dtype=torch.long, device=device)*(T_model.timestep-1)
        class_cache = torch.randint(0, 950, (cache_size,), device=device)
    
        # 10%의 인덱스를 무작위로 선택하여 1000으로 설정
        num_to_replace = int(cache_size * 0.1)  # 전체 크기의 10%
        indices = torch.randperm(cache_size)[:num_to_replace]  # 랜덤으로 인덱스 선택
        class_cache[indices] = 1000
    
        
        # img_cache = torch.load("img_cache_cache.pt")
        # t_cache = torch.load("t_cache_cache.pt")
    
        # if img_cache, t_cache exists:
        #     img_cache = torch.load("img_cache_cache.pt")
        #     t_cache = torch.load("t_cache_cache.pt")
    
        # else:
        with torch.no_grad():
            for i in range(T_model.timestep):
                start_time = time.time()
                
                start_idx = (i * args.cache_n)
                end_idx = start_idx + args.cache_n
                
                # 슬라이스 처리
                img_batch = img_cache[start_idx:end_idx]
                t_batch = t_cache[start_idx:end_idx]
                class_batch = class_cache[start_idx:end_idx]
                
                c = T_model.get_learned_conditioning(
                            {T_model.cond_stage_key: class_batch})
                
                
                img_cache[i:end_idx] = T_sampler.DDPM_target_t(img_batch, c, target_t = i)
                t_cache[start_idx:end_idx] = torch.ones(args.cache_n, dtype=torch.long, device=device)*(i)
     
                print(f"start_idx: {i}, end_idx: {end_idx}")
    
                elapsed_time = time.time() - start_time
                print(f"Iteration {i + 1}/{T_model.timestep} completed in {elapsed_time:.2f} seconds.")
    
            save_dir = f"./{args.cachedir}/{args.cache_n}"
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)
            
            # Save img_cache, t_cache, and class_cache as .pt files
            torch.save(img_cache, f"{save_dir}/img_cache_{args.cache_n}.pt")
            torch.save(t_cache, f"{save_dir}/t_cache_{args.cache_n}.pt")
            torch.save(class_cache, f"{save_dir}/class_cache_{args.cache_n}.pt")
    
        print(f"Pre-caching completed and saved to {args.cachedir}")
    
        ############################################ precacheing ##################################################


    img_cache = torch.load(f"{save_dir}/img_cache_{args.cache_n}.pt").to(device)
    t_cache = torch.load(f"{save_dir}/t_cache_{args.cache_n}.pt").to(device)
    class_cache = torch.load(f"{save_dir}/class_cache_{args.cache_n}.pt").to(device)

    

        # pt로 image_cache, t_cache 저장

    # with torch.no_grad():
    #     for i in range(0, cache_size, args.cache_n):
    #         start_time = time.time()
            
    #         # 슬라이스의 끝 인덱스가 전체 크기를 초과하지 않도록 min 사용
    #         end_idx = min(i + args.cache_n, cache_size)
            
    #         # 슬라이스 처리
    #         img_batch = img_cache[i:end_idx]
    #         t_batch = t_cache[i:end_idx]
    #         class_batch = class_cache[i:end_idx]
            
    #         c = T_model.get_learned_conditioning(
    #                     {T_model.cond_stage_key: class_batch})
            
            
    #         img_cache[i:end_idx] = T_sampler.caching_target_t(img_batch, c, target_t = t_batch)
 
    #         print(f"start_idx: {i}, end_idx: {end_idx}")

    #         elapsed_time = time.time() - start_time
    #         print(f"Iteration {i + 1}/{T_model.timestep} completed in {elapsed_time:.2f} seconds.")
            
            
            #################### X0 caching #######################
            # uc = T_model.get_learned_conditioning(
            #             {T_model.cond_stage_key: torch.tensor(img_batch.shape[0] * [1000]).to(device)}
            #         )
            
            # c = T_model.get_learned_conditioning(
            #             {T_model.cond_stage_key: class_batch})

            # img_cache[i:end_idx], _ = T_sampler.X0_DDPM(S=args.ddim_steps,
            #                                 conditioning=c,
            #                                 ddim_use_original_steps = True, #True  #### DDIM for cache??
            #                                 batch_size=img_batch.shape[0],
            #                                 shape=[3, 64, 64],
            #                                 verbose=False,
            #                                 unconditional_guidance_scale=args.CFG_scale, #우선 1로
            #                                 unconditional_conditioning=uc,
            #                                 eta=1)
                    
    ##################################

    with trange(args.total_steps, dynamic_ncols=True) as pbar:
        for step in pbar:
            optimizer.zero_grad()

            # Step 2: Randomly sample from img_cache and t_cache without shuffling
            indices = torch.randint(0, img_cache.size(0), (args.batch_size,), device=device)

            # Sample img_cache and t_cache using the random indices
            x_t = img_cache[indices]
            t = t_cache[indices]
            c = T_model.get_learned_conditioning(
                        {T_model.cond_stage_key: class_cache[indices]})
            
            # Calculate distillation loss
            output_loss, total_loss, x_prev = trainer(x_t, c, t, args.CFG_scale)

            # Backward and optimize
            total_loss.backward()
            # torch.nn.utils.clip_grad_norm_(S_model.parameters(), args.grad_clip)
            
            optimizer.step()
            lr_scheduler.step()

            ### cache update ###
            img_cache[indices] = x_prev
            t_cache[indices] -= 1
            
            # num_999 = torch.sum(t_cache == (args.T - 1)).item()

            # if num_999 < args.cache_n:
            #     missing_999 = args.cache_n - num_999
            #     non_999_indices = (t_cache != (args.T - 1)).nonzero(as_tuple=True)[0]
            #     random_indices = torch.randperm(non_999_indices.size(0), device=device)[:missing_999]
            #     selected_indices = non_999_indices[random_indices]
            #     t_cache[selected_indices] = args.T - 1
            #     img_cache[selected_indices] = torch.randn(missing_999, 3, args.img_size, args.img_size, device=device)

            # # t_cache에서 값이 0인 인덱스를 찾아 초기화
            # zero_indices = (t_cache < 0).nonzero(as_tuple=True)[0]
            # num_zero_indices = zero_indices.size(0)

            # # 0인 인덱스가 있는 경우에만 초기화 수행
            # if num_zero_indices > 0:
            #     # 0인 인덱스를 1에서 args.T-1 사이의 랜덤한 정수로 초기화
            #     t_cache[zero_indices] = torch.randint(0, args.T, size=(num_zero_indices,), dtype=torch.long, device=device)
            #     img_cache[zero_indices] = trainer.diffusion(img_cache[zero_indices],t_cache[zero_indices])

            # t_cache에서 값이 0인 인덱스를 찾아 초기화
            zero_indices = (t_cache < 0).nonzero(as_tuple=True)[0]
            num_zero_indices = zero_indices.size(0)

            # 0인 인덱스가 있는 경우에만 초기화 수행
            if num_zero_indices > 0:
                # 0인 인덱스를 1에서 args.T-1 사이의 랜덤한 정수로 초기화
                t_cache[zero_indices] = torch.ones(num_zero_indices, dtype=torch.long, device=device) *(T_model.timestep-1)
                img_cache[zero_indices] = torch.randn(num_zero_indices, T_model.channels, T_model.img_size, T_model.img_size).to(device)

            
            
            # Logging with WandB
            wandb.log({
                'distill_loss': total_loss.item(),
                'output_loss': output_loss.item()
                       }, step=step)
            pbar.set_postfix(distill_loss='%.3f' % total_loss.item())
             
            ################### Sample and save student outputs############################
            if args.sample_step > 0 and step % args.sample_step == 0:
                sample_save_images(args.num_sample_class, args.n_sample_per_class, args.sample_save_ddim_steps, args.eta, args.cfg_scale, T_model, S_model)

            ################### Save student model ################################
            if args.save_step > 0 and step % args.save_step == 0:
                save_checkpoint(S_model, lr_scheduler, optimizer, step, args.logdir)
            ################### Evaluate student model ##############################

    wandb.finish()


def main(argv):
    warnings.simplefilter(action='ignore', category=FutureWarning)
    
    # distill_caching_base()
    parser = get_parser()
    distill_args = parser.parse_args()
    seed_everything(distill_args.seed)
    distillation(distill_args)

if __name__ == '__main__':
    app.run(main)