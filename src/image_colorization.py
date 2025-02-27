"""image colorization"""

import kornia
import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn.functional as F
import tqdm

from src.coef_chrominance import COEFS
from src.dip import get_net
from src.dip.downsampler import Downsampler
from src.eccv16 import BaseColor, eccv16
from src.utils import get_params, resize_image, upsample

GPU = torch.cuda.is_available()
if GPU:
    # pylint: disable=E1101
    DTYPE = torch.cuda.FloatTensor
    # pylint: enable=E1101

else:
    DTYPE = torch.float

DEV = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")


class Colorizer:
    """
    A class for image colorization using a pre-trained ECCV16 neural network.

    Attributes:
        eccv (torch.nn.Module): An instance of the ECCV16 neural network model
            pre-trained for colorization.
        colorizer (torch.nn.Module): The ECCV16 model in evaluation mode.

    Methods:
        __init__():
            Initializes the Colorizer class, loads the pre-trained ECCV16 model,
            and sets it to evaluation mode. If a GPU is available, the model
            is transferred to the GPU.

        __call__(image: torch.tensor) -> torch.tensor:
            Performs colorization on the input grayscale image and
            returns the colorized image.
    """

    def __init__(self):
        """
        Initializes the Colorizer with a pre-trained ECCV16 model.

        - Loads the ECCV16 model using the `eccv16` function with pre-trained weights.
        - Sets the model to evaluation mode using `.eval()`.
        - Moves the model to GPU if the `GPU` flag is set to True.
        """
        self.eccv = eccv16(pretrained=True)  # Load the pre-trained ECCV16 model.
        self.colorizer = self.eccv.eval()  # Set the model to evaluation mode.
        if GPU:
            self.colorizer.cuda()  # Move the model to GPU if available.

    def __call__(self, image: torch.tensor) -> torch.tensor:
        """
        Performs colorization on a grayscale image.

        Args:
            image (torch.tensor): A grayscale image represented as a PyTorch tensor.

        Returns:
            torch.tensor: The colorized image, output by the ECCV16 model.

        Note:
            The input `image` should already be a PyTorch tensor. This method assumes
            the input is correctly formatted for the ECCV16 model.
        """
        return self.colorizer(image)  # Pass the input through the ECCV16 model.


COLORIZER = Colorizer()
BASE_COLOR = BaseColor()


def convert_to_single_channel(image):
    """
    Converts a black-and-white image with 3 dimensions to a single channel if it has 3 channels.

    Args:
        image (torch.Tensor): Input image tensor (C, H, W), (H, W, C), or (H, W).

    Returns:
        torch.Tensor: Single-channel image tensor.
    """
    if image.ndim == 3:  # Check if the image has 3 dimensions
        # Check along each dimension to find the one with 3 channels
        for dim in range(3):
            if image.shape[dim] == 3:
                return image.select(
                    dim, 0
                )  # Select the first channel along this dimension

    # If the image doesn't have 3 dimensions or channels, return it unchanged
    return image


class ECCVImage:
    """
    A class for processing and colorizing grayscale images using the ECCV16 model.

    Attributes:
        original_bw (torch.tensor): The original grayscale image normalized to the range [0, 100].
        luminance_256 (torch.tensor): Resized luminance channel of the input image (256x256).
        luminance_64 (torch.tensor): Resized luminance channel of the input image (64x64).
        proba_distrib (torch.tensor): Output probability distribution from the ECCV model.
        proba_chrom_norm (torch.tensor): Normalized chrominance channels
            derived from the ECCV output.
        proba_chrom_unnorm (torch.tensor): Unnormalized chrominance channels.
        chrom_mean_unnorm (torch.tensor): Mean chrominance values for each pixel.
        lab_mean_64 (torch.tensor): Combined LAB representation at 64x64 resolution.
        rgb_mean (torch.tensor): Mean RGB representation at 64x64 resolution.
        output_upsampled (torch.tensor): Final upsampled LAB representation at 256x256 resolution.

    Methods:
        __init__(black_image):
            Initializes the ECCVImage with a grayscale input image and processes it.
            Converts the image to luminance and prepares chrominance channels.

        process_eccv():
            Processes the input image using the ECCV model and derives chrominance channels.
            Converts the output to LAB and RGB color spaces for further processing.

        plot_eccv():
            Visualizes the colorized image in RGB format at 256x256 resolution.
    """

    def __init__(self, black_image):
        """
        Initializes the ECCVImage with a grayscale image.

        Args:
            black_image (torch.tensor): A grayscale input image to be colorized.

        Notes:
            - The input image is normalized to the range [0, 100] and resized for processing.
            - Chrominance channels are defined and processed via the ECCV model.
        """
        black_image_single = convert_to_single_channel(black_image)

        # Normalize the input image and scale it to the range [0, 100].
        black_image_norm = black_image_single.to(DEV) * (100 / 255)
        self.original_bw = black_image_norm

        # Resize the luminance channel to 256x256 and 64x64 for processing.
        self.luminance_256 = torch.clip(
            resize_image(black_image_norm, size=(256, 256)), 0, 100
        )
        self.luminance_64 = torch.clip(
            resize_image(black_image_norm, size=(64, 64)), 0, 100
        )

        # Process the image using the ECCV model to define chrominance channels.
        self.process_eccv()

    def process_eccv(self):
        """
        Processes the image using the ECCV model to generate chrominance channels.

        - Computes the probability distribution of chrominance channels using ECCV.
        - Derives normalized and unnormalized chrominance values.
        - Combines luminance and chrominance channels to form LAB and RGB representations.
        """
        # Get the probability distribution from the ECCV model.
        self.proba_distrib = torch.tensor(
            COLORIZER(self.luminance_256[None, None, :].to(DEV))
        )

        # Compute normalized and unnormalized chrominance channels.
        self.proba_chrom_norm = torch.einsum(
            "abcd,bn->nbcd", self.proba_distrib, COEFS.to(DEV)
        )
        self.proba_chrom_unnorm = BASE_COLOR.unnormalize_ab(self.proba_chrom_norm)
        self.chrom_mean_unnorm = self.proba_chrom_unnorm.sum(axis=1)  # (2,64,64)

        # Combine luminance and chrominance to form the LAB representation.
        self.lab_mean_64 = torch.cat(
            (self.luminance_64[None, None, :], self.chrom_mean_unnorm[None, :]),
            dim=1,
        )

        # Convert the LAB representation to RGB.
        self.rgb_mean = kornia.color.lab_to_rgb(self.lab_mean_64)

        # Upsample the LAB representation to 256x256 and replace
        # luminance with the higher-resolution version.
        self.output_upsampled = upsample(self.lab_mean_64)
        self.output_upsampled[:, [0], :] = self.luminance_256

    def plot_eccv(self):
        """
        Plots the colorized image in RGB format at 256x256 resolution.

        Notes:
            - Converts the upsampled LAB image to RGB and displays it using Matplotlib.
        """
        print("here")
        original_size = (self.original_bw.shape[0], self.original_bw.shape[1])
        out_original_size = F.interpolate(self.lab_mean_64.cpu(), size=original_size)
        out_original_size[:, 0, :] = self.original_bw
        out_original_size_rgb = kornia.color.lab_to_rgb(out_original_size)
        # rgb256 = kornia.color.lab_to_rgb(self.output_upsampled)
        plt.figure(figsize=(10, 10))
        plt.title("Colorization with ECCV16")
        plt.imshow(out_original_size_rgb[0, :].cpu().permute(1, 2, 0).detach().numpy())
        plt.axis("off")
        plt.gca().set_aspect("equal")
        plt.savefig("output_eccv.png")
        plt.show()


class LoriaImageColorization(ECCVImage):
    """
    A subclass of ECCVImage for image colorization using a Deep Image Prior (DIP) network.
    """

    def __init__(self, black_image):
        """
        Initializes the LoriaImageColorization class with a grayscale image.

        Args:
            black_image (torch.Tensor): Input grayscale image to be colorized.
        """
        super().__init__(black_image)

        # Initialize DIP network and related components.
        self.ones = torch.ones(1, 313, 64, 64).to(DEV)
        self.dip_net = get_net(
            32,
            "skip",
            "reflection",
            n_channels=3,
            skip_n33d=128,
            skip_n33u=128,
            skip_n11=4,
            num_scales=5,
            upsample_mode="bilinear",
        ).type(DTYPE)

        self.downsampler = Downsampler(
            n_planes=3, factor=4, kernel_type="lanczos2", phase=0.5, preserve_size=True
        ).type(DTYPE)

        self.dip_input = (
            torch.tensor(np.random.normal(size=(1, 32, 256, 256))).type(DTYPE).detach()
        ).to(DEV)

        # Target DIP image for optimization.
        self.target_dip = self.get_initialized_image()

        # Define loss function.
        self.loss_fn = torch.nn.MSELoss()
        self.out = torch.zeros(0)

    def closure(self, num_iter_active):
        """
        Performs a single step in the optimization loop.

        Args:
            num_iter_active (int): Current iteration number.
        """
        if num_iter_active:
            pass
        # Generate the DIP output.
        self.out = self.dip_net(self.dip_input)

        # Compute total loss with coupled TV and backward propagation.
        total_loss = self.loss_coupled_tv(self.out)
        total_loss.backward(retain_graph=True)
        return total_loss

    def optimize(self, lr, num_iter):
        """
        Runs the optimization loop for the DIP network.

        Args:
            lr (float): Learning rate.
            num_iter (int): Number of optimization iterations.
        """
        parameters = get_params("net", self.dip_net, self.dip_input)
        print("Starting optimization with ADAM")
        optimizer = torch.optim.Adam(parameters, lr=lr)
        print("Nombre d'itérations total :", num_iter)
        print("Optimization...")
        for j in tqdm.tqdm(range(num_iter)):
            if j < 800 or (j % 200 != 0):
                optimizer.zero_grad()
                self.closure(j)
                optimizer.step()
            else:
                new_target = self.projection_chrom(self.downsampler(self.out))
                new_target[0, 0, :] = self.luminance_64.clone().detach() / 100
                self.target_dip = new_target.clone().detach()
        print("Optimzation done!")

    def loss_coupled_tv(self, out, gamma=100):
        """
        Computes the total variation loss coupled with L2 and luminance loss.

        Args:
            out (torch.Tensor): Output of the DIP network.
            gamma (float): Weight for the TV term.

        Returns:
            torch.Tensor: Total loss value.
        """
        # Extract luminance and chrominance.
        lum = self.luminance_256[None, None, :] / 100
        ab = out[:, [1, 2], :, :]

        # Compute coupled TV loss.
        dl_h = gamma * torch.pow(lum[:, :, :, 1:] - lum[:, :, :, :-1], 2)
        dl_w = gamma * torch.pow(lum[:, :, 1:, :] - lum[:, :, :-1, :], 2)
        dab_h = torch.pow(ab[:, :, :, 1:] - ab[:, :, :, :-1], 2)
        dab_w = torch.pow(ab[:, :, 1:, :] - ab[:, :, :-1, :], 2)
        epsilon = 1e-5

        tv_c = 0.000005 * torch.sum(
            torch.sqrt(
                epsilon
                + dl_h[:, :, :-1, :]
                + dl_w[:, :, :, :-1]
                + dab_h[:, :, :-1, :]
                + dab_w[:, :, :, :-1]
            )
        )

        # Compute L2 loss and luminance loss.
        l2 = self.loss_fn(self.downsampler(out), self.target_dip)
        loss_lum = self.loss_fn(out[0, 0, :], self.luminance_256 / 100)

        return l2 + loss_lum + tv_c

    def get_initialized_image(self):
        """
        Initializes the target image for the DIP network based on the chrominance distribution.

        Returns:
            torch.Tensor: Initialized LAB image.
        """
        coefs_to_128 = 0.5 * (COEFS.to(DEV) + 1)
        coefs_a = coefs_to_128[:, 0]
        coefs_b = coefs_to_128[:, 1]
        coefs_a = coefs_a[None, :, None, None] * self.ones
        coefs_b = coefs_b[None, :, None, None] * self.ones

        ind_max = torch.argmax(self.proba_distrib, axis=1)
        chr_a = torch.gather(coefs_a, 1, ind_max.unsqueeze(2)).squeeze(2)
        chr_b = torch.gather(coefs_b, 1, ind_max.unsqueeze(2)).squeeze(2)

        initialized = torch.ones(1, 3, 64, 64).to(DEV)
        initialized[:, 1, :, :] = chr_a
        initialized[:, 2, :, :] = chr_b
        initialized[:, 0, :, :] = self.luminance_64 / 100
        return initialized

    def projection_chrom(self, image):
        """
        Projette les chrominances d'une image sur un ensemble discret de valeurs pré-définies.

        Args:
            image: Image en espace Lab.

        Returns:
            Image avec chrominances projetées.
        """
        coefs_to_128 = 0.5 * (COEFS.to(DEV) + 1)  # entre 0 et 1
        coefs_a = coefs_to_128[:, 0]
        coefs_b = coefs_to_128[:, 1]
        coefs_a = coefs_a[None, :, None, None] * self.ones
        coefs_b = coefs_b[None, :, None, None] * self.ones

        ind_min = torch.argmin(
            torch.pow(coefs_a - image[0, 1, :], 2)
            + torch.pow(coefs_b - image[0, 2, :], 2),
            dim=1,
        )

        projected_chrm_a = torch.gather(coefs_a, 1, ind_min.unsqueeze(2)).squeeze(2)
        projected_chrm_b = torch.gather(coefs_b, 1, ind_min.unsqueeze(2)).squeeze(2)

        projected = torch.ones(1, 3, 64, 64).to(DEV)
        # il ne faut pas denormaliseer
        projected[:, 1, :, :] = projected_chrm_a
        projected[:, 2, :, :] = projected_chrm_b
        projected[0, 0, :, :] = self.luminance_64 / 100

        return projected

    def plot_result(self):
        """plot the output result"""
        original_size = (self.original_bw.shape[0], self.original_bw.shape[1])
        out_original_size = F.interpolate(self.out.cpu(), size=original_size)
        out_original_size[:, 0, :] = self.original_bw
        out_original_size[:, 1:, :] = BASE_COLOR.ab_01_to_128(
            out_original_size[:, 1:, :]
        )
        out_original_size_rgb = kornia.color.lab_to_rgb(out_original_size)
        plt.figure(figsize=(10, 10))
        plt.title("Output of our method")
        plt.imshow(out_original_size_rgb[0, :].cpu().permute(1, 2, 0).detach().numpy())
        plt.axis("off")
        plt.savefig("output.png")
        plt.gca().set_aspect("equal")
        plt.show()
        return
