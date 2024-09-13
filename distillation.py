import torch
from omegaconf import OmegaConf
import numpy as np
from PIL import Image
from einops import rearrange
from torchvision.utils import make_grid

from ldm.models.diffusion.ddim import DDIMSampler
import warnings

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
import diffusers
import argparse, os, sys, glob, yaml, math, random
import time

from tqdm import trange
from diffusers.optimization import get_scheduler
from pytorch_lightning import seed_everything

from trainer import distillation_DDPM_trainer
from funcs import load_model_from_config, get_model_teacher, load_model_from_config_without_ckpt, get_model_student, initialize_params, sample_save_images, save_checkpoint, print_gpu_memory_usage, visualize_t_cache_distribution
from gpu_log import GPUMonitor

from eval_funcs import sample_and_cal_fid

device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')


def get_parser():
    parser = argparse.ArgumentParser()
    
    parser.add_argument("--seed", type=int, default=20240911, help="seed for seed_everything")
    parser.add_argument('--gpu_no', type=int, default=0, help='GPU number to use for training')

    parser.add_argument("--trainable_modules", type=tuple, default=(None,), help="Tuple of trainable modules")
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
    parser.add_argument("--lr", type=float, default=1e-4, help="Learning rate for training")
    parser.add_argument("--scale_lr", type=bool, default=False, help="Flag to scale learning rate")
    parser.add_argument("--lr_warmup_steps", type=int, default=0, help="Number of learning rate warmup steps")
    parser.add_argument("--lr_scheduler", type=str, default="constant", help="Learning rate scheduler type")
    parser.add_argument("--gradient_accumulation_steps", type=int, default=1, help="Number of gradient accumulation steps")
    parser.add_argument("--grad_clip", type=float, default=1., help="gradient norm clipping")
    parser.add_argument("--total_steps", type=int, default=800000, help='total training steps')
    parser.add_argument("--img_size", type=int, default=32, help='image size')
    parser.add_argument("--warmup", type=int, default=5000, help='learning rate warmup')
    parser.add_argument("--batch_size", type=int, default=64, help='batch size')
    parser.add_argument("--num_workers", type=int, default=4, help='workers of Dataloader')
    parser.add_argument("--ema_decay", type=float, default=0.9999, help="ema decay rate")
    parser.add_argument("--parallel", action='store_true', help='multi gpu training')
    parser.add_argument("--distill_features", action='store_true', help='perform knowledge distillation using intermediate features')
    parser.add_argument("--loss_weight", type=float, default=0.1, help="feature loss weighting")
    
    # Logging & Sampling
    parser.add_argument("--logdir", type=str, default='./logs/cin256-v2', help='log directory')
    parser.add_argument("--sample_size", type=int, default=32, help="sampling size of images")
    parser.add_argument("--sample_step", type=int, default=10000, help='frequency of sampling')
    
    # WandB 관련 FLAGS 추가
    parser.add_argument("--wandb_project", type=str, default='distill_caching_ldm', help='WandB project name')
    parser.add_argument("--wandb_run_name", type=str, default=None, help='WandB run name')
    parser.add_argument("--wandb_notes", type=str, default='', help='Notes for the WandB run')
    
    # Evaluation
    parser.add_argument("--save_step", type=int, default=50000, help='frequency of saving checkpoints, 0 to disable during training')
    parser.add_argument("--eval_step", type=int, default=100000, help='frequency of evaluating model, 0 to disable during training')
    parser.add_argument("--num_images", type=int, default=10000, help='the number of generated images for evaluation')
    parser.add_argument("--fid_use_torch", action='store_true', help='calculate IS and FID on gpu')
    parser.add_argument("--fid_cache", type=str, default='./stats/cifar10.train.npz', help='FID cache')
    
    # Caching
    parser.add_argument("--only_pre_caching", action='store_true', help='only precaching')
    
    
    parser.add_argument("--cache_n", type=int, default=64, help='size of caching data per timestep')
    parser.add_argument("--caching_batch_size", type=int, default=256, help='batch size for pre-caching')
    parser.add_argument('--cachedir', type=str, default='./cache', help='log directory')
    parser.add_argument("--is_precache", action="store_true", help="whether to perform pre-caching")



    #DDIM Sampling
    parser.add_argument("--DDIM_num_steps", type=int, default=50, help='number of DDIM samping steps')

    parser.add_argument("--num_sample_class", type=int, default=4, help='number of class for save and sampling')
    parser.add_argument("--n_sample_per_class", type=int, default=16, help='number of sample for per class in save_sample')

    parser.add_argument("--sample_save_ddim_steps", type=int, default=20, help='number of DDIM sampling steps')
    parser.add_argument("--ddim_eta", type=float, default=1.0, help='DDIM eta parameter for noise level')
    parser.add_argument("--cfg_scale", type=float, default=1, help='guidance scale for unconditional guidance, 1 or none = no guidance, 0 = uncond')

    #Directory
    # parser.add_argument('--logdir', type=str, default='./logs', help='log directory')


    
    return parser
def pre_caching(args, gpu_num, gpu_no):

    #gpu_monitor = GPUMonitor(monitoring_int
    
 
    #gpu_monitor.start("model_load_start!!")

    T_model = get_model_teacher()
    T_model = T_model.cuda(gpu_no)

    T_device = T_model.device

    T_sampler = DDIMSampler(T_model)
    #print_gpu_memory_usage('models to sampler')
    
    T_sampler.make_schedule(ddim_num_steps = args.DDIM_num_steps, ddim_eta= 1, verbose=False)

    ############################################ precacheing ##################################################
    cache_size = args.cache_n*args.T
    
    img_cache = torch.randn(cache_size, T_model.channels, T_model.image_size, T_model.image_size).to(T_device)
    t_cache = torch.ones(cache_size, dtype=torch.long, device=T_device)*(args.T-1)
    
    selected_tensor  = torch.tensor(
        [862, 43, 335, 146, 494, 491, 587, 588, 187, 961, 78, 205, 297, 214, 163, 788, 980, 507, 916, 112, 512, 589, 771, 27, 269, 386, 336, 280, 362, 510, 850, 661, 731, 613, 945, 704, 86, 160, 372, 910, 159, 493, 623, 73, 128, 234, 717, 710, 887, 423, 546, 148, 558, 358, 463, 224, 987, 960, 444, 965, 363, 854, 492, 87, 672, 870, 217, 292, 303, 508, 188, 296, 642, 349, 154, 690, 298, 670, 964, 341, 873, 236, 35, 28, 890, 698, 902, 457, 621, 629, 371, 114, 610, 186, 718, 815, 944, 832, 869, 919, 441, 394, 625, 993, 401, 650, 55, 825, 272, 233, 738, 483, 473, 8, 220, 547, 684, 533, 132, 646, 455, 895, 52, 400, 593, 943, 848, 380, 175, 951, 195, 404, 856, 464, 123, 10, 433, 283, 366, 122, 307, 460, 616, 585, 407, 785, 835, 712, 912, 397, 440, 901, 600, 732, 140, 499, 864, 653, 584, 844, 874, 420, 147, 574, 24, 183, 243, 379, 338, 699, 94, 79, 254, 458, 430, 350, 388, 711, 639, 415, 299, 412, 743, 340, 967, 17, 992, 480, 858, 393, 918, 193, 334, 324, 575, 130, 950, 759, 820, 244, 652, 171, 18, 576, 15, 581, 93, 290, 847, 505, 922, 883, 470, 293, 777, 696, 215, 322, 291, 540, 416, 40, 956, 488, 780, 184, 453, 792, 127, 200, 602, 378, 344, 273, 255, 935, 763, 714, 529, 700, 226, 76, 502, 566, 165, 106, 867, 811, 376, 802, 678, 267, 276, 767, 881, 248, 26, 567, 995, 143, 709, 124, 927, 431, 270, 29, 966, 926, 168, 769, 149, 786, 761, 14, 22, 474, 981, 257, 676, 662, 96, 872, 679, 177, 413, 928, 314, 185, 120, 687, 395, 599, 346, 737, 352, 638, 157, 716, 974, 783, 467, 697, 559, 181, 797, 111, 144, 389, 834, 715, 894, 70, 206, 666, 0, 190, 520, 142, 259, 429, 948, 729, 841, 830, 764, 232, 150, 446, 80, 782, 225, 391, 477, 720, 295, 319, 803, 182, 989, 831, 800, 166, 506, 563, 721, 135, 305, 904, 145, 427, 72, 178, 947, 975, 33, 706, 997, 60, 828, 829, 45, 432, 482, 98, 392, 846, 968, 381, 577, 57, 240, 179, 484, 167, 282, 969, 542, 768, 930, 65, 239, 359, 107, 619, 218, 824, 503, 733, 515, 958, 469, 288, 606, 439, 622, 618, 419, 971, 294, 263, 504, 247, 744, 651, 310, 806, 339, 434, 633, 204, 659, 702, 351, 85, 81, 673, 449, 591, 537, 572, 668, 227, 580, 655, 962, 724, 937, 766, 742, 194, 285, 435, 897, 462, 708, 776, 693, 192, 582, 843, 597, 437, 513, 357, 365, 398, 713, 990, 523, 946, 837, 840, 564, 608, 855, 522, 719, 849, 603, 853, 691, 550, 37, 809, 778, 89, 321, 548, 309, 102, 41, 745, 399, 631, 7, 812, 421, 554, 119, 472, 438, 32, 481, 685, 817, 490, 723, 12, 570, 9, 568, 387, 164, 211, 6, 46, 448, 695, 242, 521, 978, 814, 875, 607, 634, 931, 884, 614, 320, 251, 77, 237, 118, 810, 617, 61, 311, 703, 963, 772, 972, 878, 571, 794, 868, 67, 774, 674, 976, 955, 49, 842, 117, 216, 932, 632, 134, 109, 994, 308, 747, 245, 517, 991, 648, 249, 643, 628, 590, 90, 30, 279, 345, 770, 544, 795, 705, 126, 913, 936, 636, 985, 219, 497, 751, 383, 410, 20, 63, 424, 138, 230, 261, 235, 649, 13, 1, 929, 228, 906, 38, 560, 598, 436, 798, 375, 921, 396, 5, 354, 640, 83, 3, 624, 511, 725, 630, 826, 333, 425, 361, 411, 626, 773, 471, 556, 728, 781, 161, 278, 790, 601, 450, 384, 996, 317, 565, 489, 804, 755, 641, 4, 277, 405, 539, 819, 115, 892, 113, 551, 734, 527, 924, 325, 451, 957, 367, 342, 323, 973, 289, 356, 327, 23, 545, 941, 36, 534, 784, 11, 977, 671, 637, 536, 905, 821, 669, 369, 101, 748, 907, 370, 212, 108, 579, 920, 595, 377, 31, 390, 409, 137, 189, 2, 222, 983, 667, 557, 385, 201, 970, 586, 692, 287, 866, 796, 58, 238, 726, 984, 445, 258, 75, 92, 355, 665, 153, 300, 162, 675, 596, 208, 903, 500, 466, 442, 286, 838, 328, 54, 531, 34, 456, 877, 689, 97, 552, 891, 760, 543, 199, 418, 660, 459, 100, 19, 514, 274, 246, 681, 647, 253, 805, 645, 900, 765, 938, 752, 50, 663, 151, 683, 526, 461, 741, 917, 152, 88, 301, 68, 125, 203, 498, 275, 822, 934, 654, 56, 155, 110, 735, 131, 74, 156, 262, 443, 207, 871, 525, 818, 306, 562, 173, 454, 485, 739, 382, 677, 364, 501, 942, 402, 749, 914, 172, 813, 176, 865, 573, 753, 592, 827, 62, 48, 347, 284, 852, 82, 730, 816, 71, 518, 909, 213, 953, 21, 688, 202, 360, 882, 141, 69, 105, 898, 104, 611, 315, 403, 999, 561, 374, 694, 876, 252, 496, 754, 158, 84, 808, 982, 532, 281, 42, 264, 348, 896, 174, 373, 879, 845, 911, 406, 198, 422, 583, 59, 535, 680, 627, 923, 316, 51, 265, 620, 417, 549, 353, 727, 304, 495, 250, 475, 241, 538, 312, 682, 66, 95, 658, 426, 266, 479, 578, 889, 368, 476, 988, 801, 740, 886, 807, 787, 414, 197, 940, 210, 779, 452, 833, 701, 408, 318, 337, 209, 775, 686, 635, 231, 139, 260, 722, 664, 313, 541, 91, 756, 519, 343, 823, 857, 791, 530, 986, 949, 750, 859, 39, 615, 915, 487, 191, 899, 998, 326, 129, 121, 330, 569, 44, 863, 516, 885, 53, 553, 736, 136, 605, 16, 99, 594, 762, 302, 952, 836, 758, 486, 103, 880, 644, 746], device=T_device)
    class_cache = selected_tensor[torch.randint(0, len(selected_tensor), (cache_size,), device=T_device)]

    c_emb_cache = torch.randn(cache_size, 1, 512).to(T_device)

    # 10%의 인덱스를 무작위로 선택하여 1000으로 설정
    num_to_replace = int(cache_size * 0.1)  # 전체 크기의 10%
    indices = torch.randperm(cache_size)[:num_to_replace]  # 랜덤으로 인덱스 선택
    class_cache[indices] = 1000
    
    with torch.no_grad():
        indices = []
        
        for i in range(1,int(args.T/2)):
            # 0부터 i*n까지의 값
            indices.extend(range(i * args.cache_n))
            
            # (1000-i)*n부터 500*n까지의 값
            indices.extend(range((1000 - i) * args.cache_n-1, 500 * args.cache_n-1, -1))
            
        for i in range(int(args.T/2)):
            indices.extend(range(500 * args.cache_n))
        
        for batch_start in trange(0, cache_size, args.caching_batch_size, desc="Pre-class_caching"):
            batch_end = min(batch_start + args.caching_batch_size, cache_size)  # 인덱스 범위를 벗어나지 않도록 처리
            class_batch = class_cache[batch_start:batch_end]
            
            c = T_model.get_learned_conditioning(
                {T_model.cond_stage_key: class_batch}
            )
            
            c_emb_cache[batch_start:batch_end] = c
            
            
        # Batch size만큼의 인덱스를 뽑아오는 과정
        for batch_start in trange(0, len(indices), args.caching_batch_size, desc="Pre-caching"):
            batch_end = min(batch_start + args.caching_batch_size, len(indices))  # 인덱스 범위를 벗어나지 않도록 처리
            batch_indices = indices[batch_start:batch_end]  # Batch size만큼 인덱스 선택

            # 인덱스를 이용해 배치 선택
            img_batch = img_cache[batch_indices]
            t_batch = t_cache[batch_indices]
            c = c_emb_cache[batch_indices]
            

            x_prev, pred_x0,_ = T_sampler.cache_step(img_batch, c, t_batch, t_batch,
                                                    use_original_steps=True,
                                                    unconditional_guidance_scale=args.cfg_scale)

            # 결과를 저장
            img_cache[batch_indices] = x_prev
            t_cache[batch_indices] -= 1

            if batch_start % 100 == 0:  # 예를 들어, 100 스텝마다 시각화
                visualize_t_cache_distribution(t_cache, args.cache_n)
                
        visualize_t_cache_distribution(t_cache, args.cache_n)
        
        save_dir = f"./{args.cachedir}/{args.cache_n}"
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        
        # Save img_cache, t_cache, and class_cache as .pt files
        torch.save(img_cache, f"{save_dir}/img_cache_{args.cache_n}_{args.seed}.pt")
        torch.save(t_cache, f"{save_dir}/t_cache_{args.cache_n}_{args.seed}.pt")
        torch.save(class_cache, f"{save_dir}/class_cache_{args.cache_n}_{args.seed}.pt")
        torch.save(c_emb_cache, f"{save_dir}/c_emb_cache_{args.cache_n}_{args.seed}.pt")
        
        slice1 = img_cache[0:4]  # 첫 번째 슬라이스: 0부터 args.cache_n
        slice2 = img_cache[args.cache_n*200:args.cache_n*200+4]  # 두 번째 슬라이스: args.cache_n*200부터 args.cache_n*201
        slice3 = img_cache[args.cache_n*400:args.cache_n*400+4]  # 세 번째 슬라이스: args.cache_n*400부터 args.cache_n*401
        slice4 = img_cache[args.cache_n*600:args.cache_n*600+4]  # 네 번째 슬라이스: args.cache_n*600부터 args.cache_n*601

        # 슬라이스들을 합치기
        img_to_save = torch.cat((slice1, slice2, slice3, slice4), dim=0)
        img = T_model.decode_first_stage(img_to_save)        
        grid_T = torch.clamp((img + 1.0) / 2.0, min=0.0, max=1.0)
        grid_T = make_grid(grid_T, nrow=4)
        # 각각의 그리드를 이미지로 변환
        grid_T = 255. * rearrange(grid_T, 'c h w -> h w c').cpu().numpy()
        # 이미지로 저장 (T_model과 S_model의 결과)
        output_image_T = Image.fromarray(grid_T.astype(np.uint8))
        output_image_T_path = 'caching_image.png'
        output_image_T.save(output_image_T_path)

        # 저장 경로 설정
        save_dir = f"./{args.cachedir}/{args.cache_n}/images"
        os.makedirs(save_dir, exist_ok=True)


    print(f"Pre-caching completed and saved to {args.cachedir}")
    
def distillation(args, gpu_num, gpu_no):

    #gpu_monitor = GPUMonitor(monitoring_interval=2)

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
    
 
    #gpu_monitor.start("model_load_start!!")

    T_model = get_model_teacher()
    S_model= get_model_student()
    T_model = T_model.cuda(gpu_no)
    S_model = S_model.cuda((gpu_no + 1) % gpu_num)

    T_device = T_model.device
    S_device = S_model.device

    initialize_params(S_model)
    
    #gpu_monitor.stop("model_load_finish!!")
    
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
    
    #gpu_monitor.start("optimizer_load_start!!")
    optimizer = torch.optim.AdamW(
        trainable_params_student,
        lr=args.lr,
        betas=(args.adam_beta1, args.adam_beta2),
        weight_decay=args.adam_weight_decay,
        eps=args.adam_epsilon,
    )
    #gpu_monitor.stop("optimizer_load_finish!!")

    

    lr_scheduler = get_scheduler(
        args.lr_scheduler,
        optimizer=optimizer,
        num_warmup_steps=args.lr_warmup_steps * args.gradient_accumulation_steps,
        num_training_steps=args.total_steps
    )

    T_sampler = DDIMSampler(T_model)
    S_sampler = DDIMSampler(S_model)
    
    #print_gpu_memory_usage('models to sampler')
    
    T_sampler.make_schedule(ddim_num_steps = args.DDIM_num_steps, ddim_eta= 1, verbose=False)
    S_sampler.make_schedule(ddim_num_steps = args.DDIM_num_steps, ddim_eta= 1, verbose=False)
     
    trainer = distillation_DDPM_trainer(T_model, S_model, T_sampler, S_sampler, args.distill_features)

    if args.is_precache:
        ############################################ precacheing ##################################################
        cache_size = args.cache_n*args.T
    
        img_cache = torch.randn(cache_size, T_model.channels, T_model.image_size, T_model.image_size).to(T_device)
        t_cache = torch.ones(cache_size, dtype=torch.long, device=T_device)*(args.T-1)
        
        selected_tensor  = torch.tensor(
            [862, 43, 335, 146, 494, 491, 587, 588, 187, 961, 78, 205, 297, 214, 163, 788, 980, 507, 916, 112, 512, 589, 771, 27, 269, 386, 336, 280, 362, 510, 850, 661, 731, 613, 945, 704, 86, 160, 372, 910, 159, 493, 623, 73, 128, 234, 717, 710, 887, 423, 546, 148, 558, 358, 463, 224, 987, 960, 444, 965, 363, 854, 492, 87, 672, 870, 217, 292, 303, 508, 188, 296, 642, 349, 154, 690, 298, 670, 964, 341, 873, 236, 35, 28, 890, 698, 902, 457, 621, 629, 371, 114, 610, 186, 718, 815, 944, 832, 869, 919, 441, 394, 625, 993, 401, 650, 55, 825, 272, 233, 738, 483, 473, 8, 220, 547, 684, 533, 132, 646, 455, 895, 52, 400, 593, 943, 848, 380, 175, 951, 195, 404, 856, 464, 123, 10, 433, 283, 366, 122, 307, 460, 616, 585, 407, 785, 835, 712, 912, 397, 440, 901, 600, 732, 140, 499, 864, 653, 584, 844, 874, 420, 147, 574, 24, 183, 243, 379, 338, 699, 94, 79, 254, 458, 430, 350, 388, 711, 639, 415, 299, 412, 743, 340, 967, 17, 992, 480, 858, 393, 918, 193, 334, 324, 575, 130, 950, 759, 820, 244, 652, 171, 18, 576, 15, 581, 93, 290, 847, 505, 922, 883, 470, 293, 777, 696, 215, 322, 291, 540, 416, 40, 956, 488, 780, 184, 453, 792, 127, 200, 602, 378, 344, 273, 255, 935, 763, 714, 529, 700, 226, 76, 502, 566, 165, 106, 867, 811, 376, 802, 678, 267, 276, 767, 881, 248, 26, 567, 995, 143, 709, 124, 927, 431, 270, 29, 966, 926, 168, 769, 149, 786, 761, 14, 22, 474, 981, 257, 676, 662, 96, 872, 679, 177, 413, 928, 314, 185, 120, 687, 395, 599, 346, 737, 352, 638, 157, 716, 974, 783, 467, 697, 559, 181, 797, 111, 144, 389, 834, 715, 894, 70, 206, 666, 0, 190, 520, 142, 259, 429, 948, 729, 841, 830, 764, 232, 150, 446, 80, 782, 225, 391, 477, 720, 295, 319, 803, 182, 989, 831, 800, 166, 506, 563, 721, 135, 305, 904, 145, 427, 72, 178, 947, 975, 33, 706, 997, 60, 828, 829, 45, 432, 482, 98, 392, 846, 968, 381, 577, 57, 240, 179, 484, 167, 282, 969, 542, 768, 930, 65, 239, 359, 107, 619, 218, 824, 503, 733, 515, 958, 469, 288, 606, 439, 622, 618, 419, 971, 294, 263, 504, 247, 744, 651, 310, 806, 339, 434, 633, 204, 659, 702, 351, 85, 81, 673, 449, 591, 537, 572, 668, 227, 580, 655, 962, 724, 937, 766, 742, 194, 285, 435, 897, 462, 708, 776, 693, 192, 582, 843, 597, 437, 513, 357, 365, 398, 713, 990, 523, 946, 837, 840, 564, 608, 855, 522, 719, 849, 603, 853, 691, 550, 37, 809, 778, 89, 321, 548, 309, 102, 41, 745, 399, 631, 7, 812, 421, 554, 119, 472, 438, 32, 481, 685, 817, 490, 723, 12, 570, 9, 568, 387, 164, 211, 6, 46, 448, 695, 242, 521, 978, 814, 875, 607, 634, 931, 884, 614, 320, 251, 77, 237, 118, 810, 617, 61, 311, 703, 963, 772, 972, 878, 571, 794, 868, 67, 774, 674, 976, 955, 49, 842, 117, 216, 932, 632, 134, 109, 994, 308, 747, 245, 517, 991, 648, 249, 643, 628, 590, 90, 30, 279, 345, 770, 544, 795, 705, 126, 913, 936, 636, 985, 219, 497, 751, 383, 410, 20, 63, 424, 138, 230, 261, 235, 649, 13, 1, 929, 228, 906, 38, 560, 598, 436, 798, 375, 921, 396, 5, 354, 640, 83, 3, 624, 511, 725, 630, 826, 333, 425, 361, 411, 626, 773, 471, 556, 728, 781, 161, 278, 790, 601, 450, 384, 996, 317, 565, 489, 804, 755, 641, 4, 277, 405, 539, 819, 115, 892, 113, 551, 734, 527, 924, 325, 451, 957, 367, 342, 323, 973, 289, 356, 327, 23, 545, 941, 36, 534, 784, 11, 977, 671, 637, 536, 905, 821, 669, 369, 101, 748, 907, 370, 212, 108, 579, 920, 595, 377, 31, 390, 409, 137, 189, 2, 222, 983, 667, 557, 385, 201, 970, 586, 692, 287, 866, 796, 58, 238, 726, 984, 445, 258, 75, 92, 355, 665, 153, 300, 162, 675, 596, 208, 903, 500, 466, 442, 286, 838, 328, 54, 531, 34, 456, 877, 689, 97, 552, 891, 760, 543, 199, 418, 660, 459, 100, 19, 514, 274, 246, 681, 647, 253, 805, 645, 900, 765, 938, 752, 50, 663, 151, 683, 526, 461, 741, 917, 152, 88, 301, 68, 125, 203, 498, 275, 822, 934, 654, 56, 155, 110, 735, 131, 74, 156, 262, 443, 207, 871, 525, 818, 306, 562, 173, 454, 485, 739, 382, 677, 364, 501, 942, 402, 749, 914, 172, 813, 176, 865, 573, 753, 592, 827, 62, 48, 347, 284, 852, 82, 730, 816, 71, 518, 909, 213, 953, 21, 688, 202, 360, 882, 141, 69, 105, 898, 104, 611, 315, 403, 999, 561, 374, 694, 876, 252, 496, 754, 158, 84, 808, 982, 532, 281, 42, 264, 348, 896, 174, 373, 879, 845, 911, 406, 198, 422, 583, 59, 535, 680, 627, 923, 316, 51, 265, 620, 417, 549, 353, 727, 304, 495, 250, 475, 241, 538, 312, 682, 66, 95, 658, 426, 266, 479, 578, 889, 368, 476, 988, 801, 740, 886, 807, 787, 414, 197, 940, 210, 779, 452, 833, 701, 408, 318, 337, 209, 775, 686, 635, 231, 139, 260, 722, 664, 313, 541, 91, 756, 519, 343, 823, 857, 791, 530, 986, 949, 750, 859, 39, 615, 915, 487, 191, 899, 998, 326, 129, 121, 330, 569, 44, 863, 516, 885, 53, 553, 736, 136, 605, 16, 99, 594, 762, 302, 952, 836, 758, 486, 103, 880, 644, 746], device=T_device)
        class_cache = selected_tensor[torch.randint(0, len(selected_tensor), (cache_size,), device=T_device)]

        c_emb_cache = torch.randn(cache_size, 1, 512).to(T_device)

        # 10%의 인덱스를 무작위로 선택하여 1000으로 설정
        num_to_replace = int(cache_size * 0.1)  # 전체 크기의 10%
        indices = torch.randperm(cache_size)[:num_to_replace]  # 랜덤으로 인덱스 선택
        class_cache[indices] = 1000
        
        # #print_gpu_memory_usage('make cache')
        
        # with torch.no_grad():
        #     for i in range(args.T):
        #         start_time = time.time()
                
        #         start_idx = (i * args.cache_n)
        #         end_idx = start_idx + args.cache_n
                
        #         # 슬라이스 처리
        #         img_batch = img_cache[start_idx:end_idx]
        #         t_batch = t_cache[start_idx:end_idx]
        #         class_batch = class_cache[start_idx:end_idx]
                
        #         c = T_model.get_learned_conditioning(
        #                     {T_model.cond_stage_key: class_batch})
                
                
        #         img_cache[start_idx:end_idx] = T_sampler.DDPM_target_t(img_batch, c, target_t = i)
        #         t_cache[start_idx:end_idx] = torch.ones(args.cache_n, dtype=torch.long, device=device)*(i)
     
        #         print(f"start_idx: {start_idx}, end_idx: {end_idx}")
    
        #         elapsed_time = time.time() - start_time
        #         print(f"Iteration {i + 1}/{args.T} completed in {elapsed_time:.2f} seconds.")
    
        #     save_dir = f"./{args.cachedir}/{args.cache_n}"
        #     if not os.path.exists(save_dir):
        #         os.makedirs(save_dir)
            
        #     # Save img_cache, t_cache, and class_cache as .pt files
        #     torch.save(img_cache, f"{save_dir}/img_cache_{args.cache_n}.pt")
        #     torch.save(t_cache, f"{save_dir}/t_cache_{args.cache_n}.pt")
        #     torch.save(class_cache, f"{save_dir}/class_cache_{args.cache_n}.pt")
    
        # print(f"Pre-caching completed and saved to {args.cachedir}")
        
        # #print_gpu_memory_usage('make cache')
        with torch.no_grad():
            indices = []
            # for i in range(args.T):
            #     if (i+1) * args.cache_n > args.caching_batch_size:
            #         indices.extend(range(0, (i+1)*args.cache_n))
                    
            #     else:    
            #         start_idx = 0
            #         end_idx = (i+1) * args.cache_n

            #         img_batch = img_cache[start_idx:end_idx]
            #         t_batch = t_cache[start_idx:end_idx]
            #         class_batch = class_cache[start_idx:end_idx]
                    
            #         c = T_model.get_learned_conditioning(
            #                     {T_model.cond_stage_key: class_batch})
                    
            #         x_prev, pred_x0,_ = T_sampler.cache_step(img_batch, c, t_batch, t_batch,
            #                                                             use_original_steps=True,
            #                                                             unconditional_guidance_scale=args.cfg_scale)
                    
            #         img_cache[start_idx:end_idx]  = x_prev
            #         t_cache[start_idx:end_idx] -=1
            
            for i in range(1,int(args.T/2)):
                # 0부터 i*n까지의 값
                indices.extend(range(i * args.cache_n))
                
                # (1000-i)*n부터 500*n까지의 값
                indices.extend(range((1000 - i) * args.cache_n-1, 500 * args.cache_n-1, -1))
                
            for i in range(int(args.T/2)):
                indices.extend(range(500 * args.cache_n))
            
            for batch_start in trange(0, cache_size, args.caching_batch_size, desc="Pre-class_caching"):
                batch_end = min(batch_start + args.caching_batch_size, cache_size)  # 인덱스 범위를 벗어나지 않도록 처리
                class_batch = class_cache[batch_start:batch_end]
                
                c = T_model.get_learned_conditioning(
                    {T_model.cond_stage_key: class_batch}
                )
                
                c_emb_cache[batch_start:batch_end] = c
                
                
            # Batch size만큼의 인덱스를 뽑아오는 과정
            for batch_start in trange(0, len(indices), args.caching_batch_size, desc="Pre-caching"):
                batch_end = min(batch_start + args.caching_batch_size, len(indices))  # 인덱스 범위를 벗어나지 않도록 처리
                batch_indices = indices[batch_start:batch_end]  # Batch size만큼 인덱스 선택

                # 인덱스를 이용해 배치 선택
                img_batch = img_cache[batch_indices]
                t_batch = t_cache[batch_indices]
                c = c_emb_cache[batch_indices]
                

                x_prev, pred_x0,_ = T_sampler.cache_step(img_batch, c, t_batch, t_batch,
                                                        use_original_steps=True,
                                                        unconditional_guidance_scale=args.cfg_scale)

                # 결과를 저장
                img_cache[batch_indices] = x_prev
                t_cache[batch_indices] -= 1

                if batch_start % 100 == 0:  # 예를 들어, 100 스텝마다 시각화
                    visualize_t_cache_distribution(t_cache, args.cache_n)
                    
            visualize_t_cache_distribution(t_cache, args.cache_n)
            
            save_dir = f"./{args.cachedir}/{args.cache_n}"
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)
            
            # Save img_cache, t_cache, and class_cache as .pt files
            torch.save(img_cache, f"{save_dir}/img_cache_{args.cache_n}.pt")
            torch.save(t_cache, f"{save_dir}/t_cache_{args.cache_n}.pt")
            torch.save(class_cache, f"{save_dir}/class_cache_{args.cache_n}.pt")
            torch.save(c_emb_cache, f"{save_dir}/c_emb_cache_{args.cache_n}.pt")
            
            slice1 = img_cache[0:4]  # 첫 번째 슬라이스: 0부터 args.cache_n
            slice2 = img_cache[args.cache_n*200:args.cache_n*200+4] 
            slice3 = img_cache[args.cache_n*400:args.cache_n*400+4]  
            slice4 = img_cache[args.cache_n*600:args.cache_n*600+4]  

            # 슬라이스들을 합치기
            img_to_save = torch.cat((slice1, slice2, slice3, slice4), dim=0)
            img = T_model.decode_first_stage(img_to_save)        
            grid_T = torch.clamp((img + 1.0) / 2.0, min=0.0, max=1.0)
            grid_T = make_grid(grid_T, nrow=4)
            # 각각의 그리드를 이미지로 변환
            grid_T = 255. * rearrange(grid_T, 'c h w -> h w c').cpu().numpy()
            # 이미지로 저장 (T_model과 S_model의 결과)
            output_image_T = Image.fromarray(grid_T.astype(np.uint8))
            output_image_T_path = 'caching_image.png'
            output_image_T.save(output_image_T_path)

            # 저장 경로 설정
            save_dir = f"./{args.cachedir}/{args.cache_n}/images"
            os.makedirs(save_dir, exist_ok=True)


        print(f"Pre-caching completed and saved to {args.cachedir}")
    
    
        ############################################ precacheing ##################################################

    ###
    
    else:
        save_dir = f"./{args.cachedir}/{args.cache_n}"
        img_cache = torch.load(f"{save_dir}/img_cache_{args.cache_n}.pt").to(T_device)
        t_cache = torch.load(f"{save_dir}/t_cache_{args.cache_n}.pt").to(T_device)
        class_cache = torch.load(f"{save_dir}/class_cache_{args.cache_n}.pt").to(T_device)
        c_emb_cache = torch.load(f"{save_dir}/c_emb_cache_{args.cache_n}.pt").to(T_device)
        
        slice1 = img_cache[0:4]  # 첫 번째 슬라이스: 0부터 args.cache_n
        slice2 = img_cache[args.cache_n*200:args.cache_n*200+4]  # 두 번째 슬라이스: args.cache_n*200부터 args.cache_n*201
        slice3 = img_cache[args.cache_n*400:args.cache_n*400+4]  # 세 번째 슬라이스: args.cache_n*400부터 args.cache_n*401
        slice4 = img_cache[args.cache_n*600:args.cache_n*600+4]  # 네 번째 슬라이스: args.cache_n*600부터 args.cache_n*601

        # 슬라이스들을 합치기
        img_to_save = torch.cat((slice1, slice2, slice3, slice4), dim=0)
        img = T_model.decode_first_stage(img_to_save)        
        grid_T = torch.clamp((img + 1.0) / 2.0, min=0.0, max=1.0)
        grid_T = make_grid(grid_T, nrow=4)
        # 각각의 그리드를 이미지로 변환
        grid_T = 255. * rearrange(grid_T, 'c h w -> h w c').cpu().numpy()
        # 이미지로 저장 (T_model과 S_model의 결과)
        output_image_T = Image.fromarray(grid_T.astype(np.uint8))
        output_image_T_path = 'caching_image.png'
        output_image_T.save(output_image_T_path)

        # 저장 경로 설정
        save_dir = f"./{args.cachedir}/{args.cache_n}/images"
        os.makedirs(save_dir, exist_ok=True)
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
    #         print(f"Iteration {i + 1}/{args.T} completed in {elapsed_time:.2f} seconds.")
            
            
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
            #                                 unconditional_guidance_scale=args.cfg_scale, #우선 1로
            #                                 unconditional_conditioning=uc,
            #                                 eta=1)
    
    
    ##################################
    
    with trange(args.total_steps, dynamic_ncols=True) as pbar:
        
        for step in pbar:
            optimizer.zero_grad()

            # Step 2: Randomly sample from img_cache and t_cache without shuffling
            indices = torch.randint(0, img_cache.size(0), (args.batch_size,), device=T_device)

            # Sample img_cache and t_cache using the random indices
            x_t = img_cache[indices]
            t = t_cache[indices]
            c = c_emb_cache[indices]
            
            #gpu_monitor.start("before_forward_start!!")
            
            # Calculate distillation loss
            output_loss, total_loss, x_prev = trainer(x_t, c, t, args.cfg_scale, args.loss_weight)

            #gpu_monitor.stop("forward_stop!!")

            #gpu_monitor.start("loss backward_start!!")
            # Backward and optimize
            total_loss.backward()
            # torch.nn.utils.clip_grad_norm_(S_model.parameters(), args.grad_clip)
            #gpu_monitor.stop("loss backward_stop!!")

            #gpu_monitor.start("optimizer step_start!!")
            optimizer.step()
            lr_scheduler.step()
            #gpu_monitor.stop("optimizer step_stop!!")

            ### cache update ###
            img_cache[indices] = x_prev
            t_cache[indices] -= 1
            if step%1000 == 0:
                visualize_t_cache_distribution(t_cache, args.cache_n)
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
                # 0인 인덱스를 T-1 로 변환
                t_cache[zero_indices] = torch.ones(num_zero_indices, dtype=torch.long, device=T_device) *(args.T-1)
                img_cache[zero_indices] = torch.randn(num_zero_indices, T_model.channels, T_model.image_size, T_model.image_size).to(T_device)

            #gpu_monitor.start("cuda empty cache_start!!")
            torch.cuda.empty_cache()  # 메모리 해제
            #gpu_monitor.stop("cuda empty cache_stop!!")

            # Logging with WandB
            wandb.log({
                'distill_loss': total_loss.item(),
                'output_loss': output_loss.item()
                       }, step=step)
            pbar.set_postfix(distill_loss='%.3f' % total_loss.item())
             
            ################### Sample and save student outputs############################
            if step>0 and args.sample_step > 0 and step % args.sample_step == 0:
                sample_save_images(args.num_sample_class, args.n_sample_per_class, 
                                   args.sample_save_ddim_steps, args.ddim_eta, args.cfg_scale, 
                                   T_model, S_model, T_sampler, S_sampler, step)
                
        
            ################### Save student model ################################
            if step>0 and args.save_step > 0 and step % args.save_step == 0:
                save_checkpoint(S_model, lr_scheduler, optimizer, step, args.logdir)
            ################### Evaluate student model ##############################
            if step>0 and args.eval_step > 0 and step % args.eval_step == 0:# and step != 0:
                S_model.eval()
                
                fid_a, fid_b, fid_c = sample_and_cal_fid(model=S_model , device=device, num_images=args.num_images, ddim_eta = args.ddim_eta, cfg_scale = args.cfg_scale, DDIM_num_steps=args.DDIM_num_steps)
                
                S_model.train()
                
                metrics = {
                    'Student_FID_A': fid_a,
                    'Student_FID_B': fid_b,
                    'Student_FID_C': fid_c,
                }
                
                print(metrics)
                
                # Log metrics to wandb
                wandb.log(metrics, step=step)
    wandb.finish()



def main(argv):
    warnings.simplefilter(action='ignore', category=FutureWarning)
    
    parser = get_parser()
    distill_args = parser.parse_args(argv[1:])  # argv[1:]로 수정하여 인자 전달
    seed_everything(distill_args.seed)
    
    # GPU 번호를 argparse 인자로 받기
    gpu_no = distill_args.gpu_no
    # 전체 GPU 개수 가져오기
    gpu_num = torch.cuda.device_count()
    
    if distill_args.only_pre_caching:
        pre_caching(distill_args, gpu_num, gpu_no)
        
    else:
        distillation(distill_args, gpu_num, gpu_no)

if __name__ == '__main__':
    main(sys.argv)  # main()을 직접 호출
