import os
import cv2
import random
import numpy as np
import pandas as pd
from typing import Sequence
import matplotlib.pyplot as plt

from src.utils.AID import AID
from src.utils.extra import show_image
from src.core.config import SingleModelConfig
from src.core.transform import get_affine_transform, affine_transform

import torch
from torch.utils.data import Dataset


class DaconKeypointsDataset(Dataset):
    def __init__(
            self,
            cfg: SingleModelConfig,
            image_dir: str,
            label_df: pd.DataFrame,
            transforms: Sequence = None,
            mode: str = 'train'
    ) -> None:
        self.image_dir = image_dir
        self.df = label_df
        self.transforms = transforms

        self.cfg = cfg
        self.mode = mode
        self.kpd = cfg.kpd
        self.debug = cfg.debug
        self.shift = cfg.shift
        self.num_joints = cfg.num_joints
        self.flip_pairs = cfg.flip_pair
        self.image_size = cfg.image_size
        self.heatmap_size = cfg.output_size
        self.sigma = cfg.sigma
        self.target_type = cfg.target_type
        if cfg.aid:
            self.aid = AID()
        else:
            self.aid = None

        self.use_different_joints_weight = cfg.use_different_joints_weight
        self.joints_weight = cfg.joints_weight

    def __len__(self) -> int:
        return self.df.shape[0]

    def __getitem__(self, index: int):
        image_id = self.df.iloc[index, 0]
        if image_id.startswith("TRAIN") or image_id.startswith("TEST"): yoga = True
        else: yoga = False

        keypoints = self.df.iloc[index, 1:].values.reshape(-1, 2).astype(np.float32)
        keypoints = np.concatenate([keypoints, np.ones((24, 1))], axis=1)

        # define bbox
        xmin = np.min(keypoints[:, 0])
        xmax = np.max(keypoints[:, 0])
        width = xmax - xmin if xmax > xmin else 20
        center = (xmin + xmax) / 2.
        xmin = int(center - width / 2. * 1.2)
        xmax = int(center + width / 2. * 1.2)

        ymin = np.min(keypoints[:, 1])
        ymax = np.max(keypoints[:, 1])
        height = ymax - ymin if ymax > ymin else 20
        center = (ymin + ymax) / 2.
        ymin = int(center - height / 2. * 1.2)
        ymax = int(center + height / 2. * 1.2)

        x, y, w, h = xmin, ymin, xmax - xmin, ymax - ymin
        aspect_ratio = self.image_size[1] / self.image_size[0]
        centre = np.array([x + w * .5, y + h * .5])
        if w > aspect_ratio * h:
            h = w * 1.0 / aspect_ratio
        # if w < aspect_ratio * h:
        elif w < aspect_ratio * h:
            w = h * aspect_ratio

        scale = np.array([w, h]) * 1.25
        rotation = 0

        if not yoga:
            image = cv2.imread(os.path.join(self.image_dir, image_id), cv2.COLOR_BGR2RGB)
        else:
            image_dir = os.path.join(self.cfg.main_dir, "data", "yoga")
            image = cv2.imread(os.path.join(image_dir, image_id), cv2.COLOR_BGR2RGB)

        # if it's train mode
        if self.mode == 'train':
            scale_factor = 0.3
            rotate_factor = 45
            scale = scale * np.clip(np.random.randn() * scale_factor + 1, 1 - scale_factor, 1 + scale_factor)
            rotation = np.clip(np.random.randn() * rotate_factor, -rotate_factor * 2,
                               rotate_factor * 2) if random.random() <= 0.5 else 0

            # lr flipping
            if np.random.random() <= 0.5:
                image = np.flip(image, 1)
                centre[0] = image.shape[1] - 1 - centre[0]

                keypoints[:, 0] = image.shape[1] - 1 - keypoints[:, 0]
                for (q, w) in self.flip_pairs:
                    keypoints_q, keypoints_w = keypoints[q, :].copy(), keypoints[w, :].copy()
                    keypoints[w, :], keypoints[q, :] = keypoints_q, keypoints_w

        trans = get_affine_transform(centre, scale, rotation, (self.image_size[1], self.image_size[0]))
        cropped_image = cv2.warpAffine(image, trans, (self.image_size[1], self.image_size[0]), flags=cv2.INTER_LINEAR)
        for j in range(self.num_joints):
            if keypoints[j, 2] > 0:
                keypoints[j, :2] = affine_transform(keypoints[j, :2], trans)
                keypoints[j, 2] *= ((keypoints[j, 0] >= 0) & (keypoints[j, 0] < self.image_size[1]) \
                                    & (keypoints[j, 1] >= 0) & (keypoints[j, 1] < self.image_size[0]))

        target, target_weight = self.generate_target(keypoints[:, :2], keypoints[:, 2])
        target = torch.from_numpy(target)
        target_weight = torch.from_numpy(target_weight)

        if self.transforms is not None:
            cropped_image = self.transforms(image=cropped_image)['image']

        if self.aid is not None:
            cropped_image = self.aid(cropped_image)

        # random horizontal & vertical shifting
        if self.mode == 'train' and self.shift and np.random.random() <= 0.5:
            cropped_image, keypoints = self.shift_images(cropped_image, keypoints)

        if self.debug:
            show_image(cropped_image, keypoints)

            target_heatmap = self.render_gaussian_heatmap(keypoints[:, :2], output_shape=self.heatmap_size)
            visualize_heatmap = target_heatmap  # * 255.
            visualize_heatmap = visualize_heatmap.astype('uint8')[0]
            visualize_heatmap = np.max(visualize_heatmap, axis=2)
            visualize_heatmap = cv2.applyColorMap(visualize_heatmap, cv2.COLORMAP_JET)
            fig, ax = plt.subplots(dpi=200)
            ax.imshow(visualize_heatmap)
            ax.axis('off')
            plt.show()

        sample = {
            'image': torch.from_numpy(cropped_image).float().permute(2, 0, 1),
            'keypoints': torch.from_numpy(keypoints).float(),
            'target': target,
            'target_weight': target_weight
        }
        return sample

    def shift_images(self, image, keypoints, max_v=25, max_h=25):
        shift_v = np.random.randint(low=-max_v, high=max_v, size=1)
        shift_h = np.random.randint(low=-max_h, high=max_h, size=1)

        m = np.array([
            [1, 0, shift_h],
            [0, 1, shift_v]
        ]).astype(np.float32)

        rows, cols = image.shape[:-1]
        image = cv2.warpAffine(image, m, (cols, rows))

        for j in range(len(keypoints)):
            if keypoints[j, 2] > 0:
                keypoints[j, :2] = affine_transform(keypoints[j, :2], m)
                keypoints[j, 2] *= ((keypoints[j, 0] >= 0) & (keypoints[j, 0] < self.image_size[1]) \
                                    & (keypoints[j, 1] >= 0) & (keypoints[j, 1] < self.image_size[0]))
        return image, keypoints

    # https://github.com/leoxiaobin/deep-high-resolution-net.pytorch/blob/master/lib/dataset/JointsDataset.py
    def generate_target(self, joints, joints_vis):
        '''
        :param joints:  [num_joints, 3]
        :param joints_vis: [num_joints, 3]
        :return: target, target_weight(1: visible, 0: invisible)
        '''
        target_weight = np.ones((self.num_joints, 1), dtype=np.float32)
        target_weight[:, 0] = joints_vis

        if self.target_type == 'gaussian':
            target = np.zeros((self.num_joints,
                               self.heatmap_size[0],
                               self.heatmap_size[1]),
                              dtype=np.float32)
            tmp_size = self.sigma * 3

            for joint_id in range(self.num_joints):
                feat_stride = self.image_size / self.heatmap_size
                mu_x = int(joints[joint_id][0] / feat_stride[0] + 0.5)
                mu_y = int(joints[joint_id][1] / feat_stride[1] + 0.5)
                # Check that any part of the gaussian is in-bounds
                ul = [int(mu_x - tmp_size), int(mu_y - tmp_size)]
                br = [int(mu_x + tmp_size + 1), int(mu_y + tmp_size + 1)]
                if ul[0] >= self.heatmap_size[1] or ul[1] >= self.heatmap_size[0] \
                        or br[0] < 0 or br[1] < 0:
                    # If not, just return the image as is
                    target_weight[joint_id] = 0
                    continue

                # # Generate gaussian
                size = 2 * tmp_size + 1
                x = np.arange(0, size, 1, np.float32)
                y = x[:, np.newaxis]
                x0 = y0 = size // 2
                # The gaussian is not normalized, we want the center value to equal 1
                g = np.exp(- ((x - x0) ** 2 + (y - y0) ** 2) / (2 * self.sigma ** 2))

                # Usable gaussian range
                g_x = max(0, -ul[0]), min(br[0], self.heatmap_size[1]) - ul[0]
                g_y = max(0, -ul[1]), min(br[1], self.heatmap_size[0]) - ul[1]
                # Image range
                img_x = max(0, ul[0]), min(br[0], self.heatmap_size[1])
                img_y = max(0, ul[1]), min(br[1], self.heatmap_size[0])

                v = target_weight[joint_id]
                if v > 0.5:
                    target[joint_id][img_y[0]:img_y[1], img_x[0]:img_x[1]] = \
                        g[g_y[0]:g_y[1], g_x[0]:g_x[1]]

        elif self.target_type == 'offset':
            target = np.zeros((self.num_joints,
                               3,
                               self.heatmap_size[0] *
                               self.heatmap_size[1]),
                              dtype=np.float32)
            feat_width = self.heatmap_size[1]
            feat_height = self.heatmap_size[0]
            feat_x_int = np.arange(0, feat_width)
            feat_y_int = np.arange(0, feat_height)
            feat_x_int, feat_y_int = np.meshgrid(feat_x_int, feat_y_int)
            feat_x_int = feat_x_int.reshape((-1,))
            feat_y_int = feat_y_int.reshape((-1,))
            kps_pos_distance_x = self.kpd
            kps_pos_distance_y = self.kpd
            feat_stride = (self.image_size - 1.0) / (self.heatmap_size - 1.0)
            for joint_id in range(self.num_joints):
                mu_x = joints[joint_id][0] / feat_stride[0]
                mu_y = joints[joint_id][1] / feat_stride[1]

                x_offset = (mu_x - feat_x_int) / kps_pos_distance_x
                y_offset = (mu_y - feat_y_int) / kps_pos_distance_y

                dis = x_offset ** 2 + y_offset ** 2
                keep_pos = np.where((dis <= 1) & (dis >= 0))[0]
                v = target_weight[joint_id]
                if v > 0.5:
                    target[joint_id, 0, keep_pos] = 1
                    target[joint_id, 1, keep_pos] = x_offset[keep_pos]
                    target[joint_id, 2, keep_pos] = y_offset[keep_pos]
            target = target.reshape((self.num_joints * 3, self.heatmap_size[0], self.heatmap_size[1]))

        if self.use_different_joints_weight:
            target_weight = np.multiply(target_weight, self.joints_weight)

        return target, target_weight


class DaconKeypointsBBoxTestDataset(Dataset):
    def __init__(
            self, image_size,
            submission_df, transforms=None,
    ) -> None:
        self.df = submission_df
        self.image_size = image_size
        self.transforms = transforms

    def __len__(self) -> int:
        return self.df.shape[0]

    def __getitem__(self, index: int):
        image_path = self.df.iloc[index, 0]

        img_name = image_path.split('/')[-1]
        if img_name[:3] in ["649", "650", "665", "666"]:
            offset_h = 380
            offset_w = int(offset_h * 1.333)
        elif img_name[:3] in ["785", "786"]:
            offset_h = 220
            offset_w = int(offset_h * 1.333)
        else:
            offset_w = 300
            offset_h = int(offset_w * 1.333)

        image = cv2.imread(image_path, cv2.COLOR_BGR2RGB)
        image_centre = np.array(image.shape[:-1]) // 2

        x1, y1, x2, y2 = self.df.iloc[index, 1:]
        bbox_centre = np.array([
            (x1 + x2) // 2,
            (y1 + y2) // 2
        ])

        cropped_y2 = np.clip(bbox_centre[1] + offset_h, 0, image.shape[0])
        cropped_y1 = np.clip(bbox_centre[1] - offset_h, 0, image.shape[0])
        cropped_x2 = np.clip(bbox_centre[0] + offset_w, 0, image.shape[1])
        cropped_x1 = np.clip(bbox_centre[0] - offset_w, 0, image.shape[1])

        x, y, w, h = cropped_x1, cropped_y1, cropped_x2 - cropped_x1, cropped_y2 - cropped_y1
        aspect_ratio = self.image_size[1] / self.image_size[0]
        centre = np.array([x + w * .5, y + h * .5])
        if w > aspect_ratio * h:
            h = w * 1.0 / aspect_ratio
        elif w < aspect_ratio * h:
            w = h * aspect_ratio

        image_centre = np.array([cropped_y1, cropped_x1])
        scale = np.array([w, h])
        rotation = 0

        trans = get_affine_transform(centre, scale, rotation, (self.image_size[1], self.image_size[0]))
        cropped_img = cv2.warpAffine(image, trans, (self.image_size[1], self.image_size[0]), flags=cv2.INTER_LINEAR)
        cropped_img_shape = np.array([h, w])

        if self.transforms:
            transposed_img = self.transforms(image=cropped_img)['image']

        sample = {
            'transposed_img': torch.from_numpy(transposed_img).float().permute(2, 0, 1),
            'centre': torch.from_numpy(centre).float(),
            'scale': torch.from_numpy(scale).float()
        }
        return sample


if __name__ == "__main__":
    from torch.utils.data import DataLoader

    main_dir = '/Volumes/MAH_1T/Git/Pytorch/[Dacon]Motion-keypoints'
    cfg = SingleModelConfig(main_dir=main_dir, shift=False, target_type='gaussian')

    data_path = os.path.join(main_dir, 'data')
    train_path = os.path.join(data_path, "train")
    train_df = pd.read_csv(os.path.join(train_path, "train_df.csv"))

    train_ds = DaconKeypointsDataset(cfg, os.path.join(train_path, "imgs"), train_df.iloc[:, ], transforms=None, mode='valid')
    train_dl = DataLoader(train_ds, batch_size=1, shuffle=True)

    sample = next(iter(train_dl))
    image = sample["image"].detach().cpu()[0].permute(1, 2, 0).numpy()
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    image = image.astype(np.uint8)

    heatmap = sample['target']
    heatmap = heatmap.detach().cpu().permute(0, 2, 3, 1).numpy() * 255.
    heatmap = heatmap.astype('uint8')[0]
    heatmap = np.max(heatmap, axis=2)
    heatmap = cv2.applyColorMap(heatmap, cv2.COLORMAP_JET)
    fig, ax = plt.subplots(nrows=1, ncols=2, dpi=200)
    ax[0].imshow(image)
    ax[0].axis('off')
    ax[1].imshow(heatmap)
    ax[1].axis('off')
    plt.tight_layout()
    plt.show()