"""
Copyright (C) 2019 NVIDIA Corporation.  All rights reserved.
Licensed under the CC BY-NC-SA 4.0 license
(https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode).
"""
import os.path
from PIL import Image

import torch.utils.data as data
from glob import glob
from skimage.io import imread
import skimage.color as color
from skimage.util import invert
import numpy as np


def default_loader(path):
    return Image.open(path).convert('RGB')

def default_loader_custom(path):
    pic = imread(path)
    class_name = get_class(path)
    if class_name == "malaria":
        pic = color.rgb2grey(pic)
        pic = invert(pic)
    elif class_name == "Human_HT29_colon-cancer":
        pic = color.rgb2grey(pic)
    elif class_name == "dp":
        pic = color.rgba2rgb(pic)
        pic = color.rgb2grey(pic)

    if (len(pic.shape)==2):
        pic = pic.reshape((pic.shape[0], pic.shape[1],1))
        pic = np.repeat(pic, 3, axis=-1)
    if (pic.shape[0]==3):
        #print("**************3 IS BACK: ",pic.shape)
        pic = pic.transpose() #Not sure this is correct to get from (y,x,3) to (3,y,x)
    return pic

def get_class(path):
    return path.split('/')[-2]

def default_filelist_reader(filelist):
    im_list = []
    with open(filelist, 'r') as rf:
        for line in rf.readlines():
            im_path = line.strip()
            im_list.append(im_path)
    return im_list


class ImageLabelFilelist(data.Dataset):
    def __init__(self,
                 root,
                 filelist,
                 transform=None,
                 filelist_reader=default_filelist_reader,
                 loader=default_loader,
                 return_paths=False):
        self.root = root
        self.im_list = filelist_reader(os.path.join(filelist))
        self.transform = transform
        self.loader = loader
        self.classes = sorted(
            list(set([path.split('/')[0] for path in self.im_list])))
        self.class_to_idx = {self.classes[i]: i for i in
                             range(len(self.classes))}
        self.imgs = [(im_path, self.class_to_idx[im_path.split('/')[0]]) for
                     im_path in self.im_list]
        self.return_paths = return_paths
        print('Data loader')
        print("\tRoot: %s" % root)
        print("\tList: %s" % filelist)
        print("\tNumber of classes: %d" % (len(self.classes)))

    def __getitem__(self, index):
        im_path, label = self.imgs[index]
        path = os.path.join(self.root, im_path)
        img = self.loader(path)
        if self.transform is not None:
            img = self.transform(img)
        if self.return_paths:
            return img, label, path
        else:
            return img, label

    def __len__(self):
        return len(self.imgs)

class ImageLabelFilelistCustom(data.Dataset):
    """
    ToDo: If we want to load content and target
    Params:
        path:   Leads from executing script to the path with the subfolders of classes.
                The class is labeled after it's folders name.
    """

    def __init__(self,
                 root=".",
                 path="",
                 transform=None,
                 loader=default_loader_custom,
                 num_classes = None,
                 return_paths=False):

        
        self.classes = next(os.walk(path))[1]
        self.imlist = []
        self.class_to_idx = {self.classes[i]: i for i in range(len(self.classes))}
        for d in self.classes:
                impath = os.path.join(path, d)
                impathTIF = os.path.join(impath, "*.tif")
                impathPNG = os.path.join(impath, "*.png")
                impathDIB = os.path.join(impath, "*.dib")
                self.imlist += glob(impathTIF)
                self.imlist += glob(impathPNG)
                self.imlist += glob(impathDIB)
        self.imgs = [(im_path, self.class_to_idx[im_path.split('/')[-2]]) for im_path in self.imlist]

        self.root = root #Do I need this?

        self.transform = transform
        self.loader = loader
        self.return_paths = return_paths
        print('Data loader')
        print("\tRoot: %s" % root)
        print("\tNumber of images: %d" % (len(self.imgs)))
        print("\tClasses: ",self.classes)
        print("\tNumber of classes: %d" % (len(self.classes)))
        if ((num_classes != None) and (num_classes != len(self.classes))):
            print("------------------WARNING----------------")
            print("It seems you have specified to have %d classes in the conf. file but %d classes were read" % (num_classes, len(self.classes)))

    def __getitem__(self, index):
        im_path, label = self.imgs[index]
        path = os.path.join(self.root, im_path)
        img = self.loader(path)
        if self.transform is not None:
            img = self.transform(img)
        if self.return_paths:
            return img, label, path
        else:
            return img, label

    def __len__(self):
        return len(self.imgs)