import os
import sys
import random
import argparse
from PIL import Image
import numpy as np
from omegaconf import OmegaConf
import torch 

import os
os.environ["KMP_DUPLICATE_LIB_OK"]="TRUE"

from mvdream.camera_utils import get_camera
from mvdream.ldm.util import instantiate_from_config
from mvdream.ldm.models.diffusion.ddim import DDIMSampler
from mvdream.model_zoo import build_model

def set_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def t2i(model, image_size, prompt, uc, sampler, step=20, scale=7.5, batch_size=8, ddim_eta=0., dtype=torch.float32, device="cuda", camera=None, num_frames=1, x0=None):
    if type(prompt)!=list:
        prompt = [prompt]
    with torch.no_grad(), torch.autocast(device_type=device, dtype=dtype):
        c = model.get_learned_conditioning(prompt).to(device)
        c_ = {"context": c.repeat(batch_size,1,1)}
        uc_ = {"context": uc.repeat(batch_size,1,1)}
        if camera is not None:
            c_["camera"] = uc_["camera"] = camera
            c_["num_frames"] = uc_["num_frames"] = num_frames

        # 4 channels of size 32x32, batch_size = 4 (4 frames per batch)
        shape = [4, image_size // 8, image_size // 8]
        samples_ddim, intermediaries = sampler.sample(S=step, conditioning=c_,
                                        batch_size=batch_size, shape=shape,
                                        verbose=True,
                                        log_every_t=2, 
                                        unconditional_guidance_scale=scale,
                                        unconditional_conditioning=uc_,
                                        eta=ddim_eta, x_T=None, x0=x0)
        
        decoded_intermediaries = []
        for inter_samples_ddim in intermediaries["x_inter"]:
            # Decode (upscale) the images
            x_sample = model.decode_first_stage(inter_samples_ddim) 
            x_sample = torch.clamp((x_sample + 1.0) / 2.0, min=0.0, max=1.0)
            x_sample = 255. * x_sample.permute(0,2,3,1).cpu().numpy()
            decoded_intermediaries.append(list(x_sample.astype(np.uint8)))

        x_sample = model.decode_first_stage(samples_ddim)
        x_sample = torch.clamp((x_sample + 1.0) / 2.0, min=0.0, max=1.0)
        x_sample = 255. * x_sample.permute(0,2,3,1).cpu().numpy()
        
    return (list(x_sample.astype(np.uint8)), decoded_intermediaries)

def test(sample, fname):
    sample = 255. * sample.permute(0,2,3,1).cpu().numpy()
        
    images = list(sample.astype(np.uint8))

    images = np.concatenate(images, 1)
    Image.fromarray(images).save(fname)

from diffusers import AutoencoderKL 

vae = AutoencoderKL.from_pretrained("CompVis/stable-diffusion-v1-4", subfolder="vae", torch_dtype=torch.float32).to("cuda")
from torchvision import transforms as tfms  

def pil_to_latents(image):
    '''
    Function to convert image to latents     
    '''     
    init_image = tfms.ToTensor()(image).unsqueeze(0) * 2.0 - 1.0
    print(init_image.shape)
    init_image = init_image.to(device="cuda", dtype=torch.float32)
    init_latent_dist = vae.encode(init_image).latent_dist.sample() * 0.18215     
    return init_latent_dist  

if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--model_name", type=str, default="sd-v2.1-base-4view", help="load pre-trained model from hugginface")
    parser.add_argument("--config_path", type=str, default=None, help="load model from local config (override model_name)")
    parser.add_argument("--ckpt_path", type=str, default=None, help="path to local checkpoint")
    parser.add_argument("--text", type=str, default="a toy dinosaur trex")#"an astronaut riding a horse")
    parser.add_argument("--suffix", type=str, default=", 3d asset")
    parser.add_argument("--size", type=int, default=256)
    parser.add_argument("--num_frames", type=int, default=4, help="num of frames (views) to generate")
    parser.add_argument("--use_camera", type=int, default=1)
    parser.add_argument("--camera_elev", type=int, default=15)
    parser.add_argument("--camera_azim", type=int, default=90)
    parser.add_argument("--camera_azim_span", type=int, default=360)
    parser.add_argument("--seed", type=int, default=23)
    parser.add_argument("--fp16", action="store_true")
    parser.add_argument("--device", type=str, default='cuda')
    args = parser.parse_args()

    dtype = torch.float16 if args.fp16 else torch.float32
    device = args.device
    batch_size = max(4, args.num_frames)

    print("load t2i model ... ")
    if args.config_path is None:
        model = build_model(args.model_name, ckpt_path=args.ckpt_path)
    else:
        assert args.ckpt_path is not None, "ckpt_path must be specified!"
        config = OmegaConf.load(args.config_path)
        model = instantiate_from_config(config.model)
        model.load_state_dict(torch.load(args.ckpt_path, map_location='cpu'))
    model.device = device
    model.to(device)
    model.eval()

    sampler = DDIMSampler(model)
    uc = model.get_learned_conditioning( [""] ).to(device)
    print("load t2i model done . ")

    # Load images

    x0_imgs = []
    for i in range(4):
        x0_img = np.array(Image.open("Dinosaur_test_bck.png"))[:, i*256:((i+1)*256), :3].astype(np.float32)
        #x0_img = Image.open("Dinosaur_test_bck.png")[:, i*256:((i+1)*256), :3].astype(np.float32)
        x0_imgs.append(x0_img)

    x0_img = np.stack(x0_imgs, axis=0)

    for idx, img in enumerate(x0_imgs):
        #images = np.concatenate(x0_img, 1)
        Image.fromarray(img.astype(np.uint8)).save(f"TestReshape{idx}.png")

    x0_img = x0_img.transpose(0, 3, 1, 2)
    
    x0_img = torch.from_numpy(x0_img).to("cuda")
    # Requires shape (1, 3, 128, 128) -> (#batches, #chans (RGB), w, h)

    with torch.no_grad(), torch.autocast(device_type=device, dtype=dtype):
        x0 = model.encode_first_stage(x0_img)
        x0 = model.get_first_stage_encoding(x0)
        #x0 = x0.expand(4, -1, -1, -1) # Tile the image to simulate multiple tiles for now.

        x_sample = model.decode_first_stage(x0)
        x_sample = torch.clamp((x_sample + 1.0) / 2.0, min=0.0, max=1.0)
        x_sample = 255. * x_sample.permute(0,2,3,1).cpu().numpy()
        
        images = list(x_sample.astype(np.uint8))

        images = np.concatenate(images, 1)
        Image.fromarray(images).save(f"EncDecoded.png")
    #c = 1/0
    # pre-compute camera matrices
    if args.use_camera:
        camera = get_camera(args.num_frames, elevation=args.camera_elev, 
                azimuth_start=args.camera_azim, azimuth_span=args.camera_azim_span)
        camera = camera.repeat(batch_size//args.num_frames,1).to(device)
    else:
        camera = None
    
    t = args.text + args.suffix
    set_seed(args.seed)
    images = []
    #for j in range(3):
    img, inter_images = t2i(model, args.size, t, uc, sampler, step=50, scale=10, batch_size=batch_size, ddim_eta=0.0, 
            dtype=dtype, device=device, camera=camera, num_frames=args.num_frames, x0=x0)
    #    img = np.concatenate(img, 1)
    #    images.append(img)
    
    for idx, inter_img in enumerate(inter_images):
        images = np.concatenate(inter_img, 1)
        Image.fromarray(images).save(f"Dinosaur_HalfMask_inter_{idx}.png")

    Image.fromarray(images).save(f"sampleWithMask.png")