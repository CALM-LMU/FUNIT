"""
Copyright (C) 2019 NVIDIA Corporation.  All rights reserved.
Licensed under the CC BY-NC-SA 4.0 license
(https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode).
"""
import numpy as np

import torch
from torch import nn
from torch import autograd

from blocks import LinearBlock, ResBlocks, ActFirstResBlock, InceptionBlock, Conv2dBlock

from debugUtils import Debugger

from customLosses import gradient_penalty

PREFIX = "networks.py"
KERNEL_SIZE_7 = 3
KERNEL_SIZE_4 = 3
KERNEL_SIZE_5 = 3

#Conv2dBlock = InceptionBlock
InceptionBlock = Conv2dBlock

debug = Debugger()

def assign_adain_params(adain_params, model):
    # assign the adain_params to the AdaIN layers in model
    for m in model.modules():
        if m.__class__.__name__ == "AdaptiveInstanceNorm2d":
            mean = adain_params[:, :m.num_features]
            std = adain_params[:, m.num_features:2*m.num_features]
            m.bias = mean.contiguous().view(-1).float()
            m.weight = std.contiguous().view(-1).float()
            if adain_params.size(1) > 2*m.num_features:
                adain_params = adain_params[:, 2*m.num_features:]


def get_num_adain_params(model):
    # return the number of AdaIN parameters needed by the model
    num_adain_params = 0
    for m in model.modules():
        if m.__class__.__name__ == "AdaptiveInstanceNorm2d":
            num_adain_params += 2*m.num_features
    return num_adain_params


class GPPatchMcResDis(nn.Module):
    def __init__(self, hp):
        super(GPPatchMcResDis, self).__init__()

        #InceptionBlock = Conv2dBlock

        assert hp['n_res_blks'] % 2 == 0, 'n_res_blk must be multiples of 2'
        self.n_layers = hp['n_res_blks'] // 2
        nf = hp['nf']
        input_channels=hp['input_nc']
        cnn_f = [Conv2dBlock(input_channels, nf, KERNEL_SIZE_7, 1, 3,
                             pad_type='reflect',
                             norm='none',
                             activation='none')]
        for i in range(self.n_layers -1 ):
            nf_out = np.min([nf * 2, 1024])
            cnn_f += [ActFirstResBlock(nf, nf, None, 'lrelu', 'none')]
            cnn_f += [ActFirstResBlock(nf, nf_out, None, 'lrelu', 'none')]
            cnn_f += [nn.ReflectionPad2d(1)]
            cnn_f += [nn.AvgPool2d(kernel_size=3, stride=2)]
            nf = np.min([nf * 2, 1024])
        nf_out = np.min([nf * 2, 1024])
        cnn_f += [ActFirstResBlock(nf, nf, None, 'lrelu', 'none')]
        cnn_f += [ActFirstResBlock(nf, nf_out, None, 'lrelu', 'none')]
        cnn_c = [Conv2dBlock(nf_out, hp['num_classes'], 1, 1,
                             norm='none',
                             activation='lrelu',
                             activation_first=True)]
        self.cnn_f = nn.Sequential(*cnn_f)
        self.cnn_c = nn.Sequential(*cnn_c)

        #self.register_backward_hook(debug.printgradnorm)
        self.debug = Debugger()

    def forward(self, x, y):
        #print("FORWARD ",self.__class__.__name__)
        assert(x.size(0) == y.size(0))
        feat = self.cnn_f(x)
        out = self.cnn_c(feat)
        index = torch.LongTensor(range(out.size(0))).cuda()
        out = out[index, y, :, :]
        return out, feat

    def calc_dis_fake_loss(self, input_fake, input_label):
        #self.debug.printCheckpoint(self.calc_dis_fake_loss)
        resp_fake, gan_feat = self.forward(input_fake, input_label)
        total_count = torch.tensor(np.prod(resp_fake.size()),
                                   dtype=torch.float).cuda()
        fake_loss = torch.nn.ReLU()(1.0 + resp_fake).mean()
        correct_count = (resp_fake < 0).sum()
        fake_accuracy = correct_count.type_as(fake_loss) / total_count
        return fake_loss, fake_accuracy, resp_fake

    def calc_dis_real_loss(self, input_real, input_label):
        #self.debug.printCheckpoint(self.calc_dis_real_loss)
        debug = Debugger(self.calc_dis_real_loss, self, PREFIX)
        resp_real, gan_feat = self.forward(input_real, input_label)
        total_count = torch.tensor(np.prod(resp_real.size()),
                                   dtype=torch.float).cuda()
        real_loss = torch.nn.ReLU()(1.0 - resp_real).mean()
        correct_count = (resp_real >= 0).sum()
        real_accuracy = correct_count.type_as(real_loss) / total_count
        return real_loss, real_accuracy, resp_real

    def calc_gen_loss(self, input_fake, input_fake_label):
        #self.debug.printCheckpoint(self.calc_gen_loss)
        resp_fake, gan_feat = self.forward(input_fake, input_fake_label)
        #print("resp_fake: max: %d, min: %d" % (resp_fake.max(), resp_fake.min()))
        #print("gan_feat: max: %d, min: %d" % (gan_feat.max(), gan_feat.min()))
        #print("input_fake: max: %d, min: %d" % (input_fake.max(), input_fake.min()))
        total_count = torch.tensor(np.prod(resp_fake.size()),
                                   dtype=torch.float).cuda()
        loss = -resp_fake.mean()
        #print("CALC_GEN_LOSS: ",loss)
        correct_count = (resp_fake >= 0).sum()
        #print("CORRECT COUNT: %d, TOTAL COUNT: %d" % (correct_count, total_count))
        accuracy = correct_count.type_as(loss) / total_count
        #print("ACC: ",accuracy)
        return loss, accuracy, gan_feat

    def calc_grad2(self, d_out, x_in):
        #self.debug.printCheckpoint(self.calc_grad2)
        batch_size = x_in.size(0)
        grad_dout = autograd.grad(outputs=d_out.mean(),
                                  inputs=x_in,
                                  create_graph=True,
                                  retain_graph=True,
                                  only_inputs=True)[0]
        grad_dout2 = grad_dout.pow(2)
        assert (grad_dout2.size() == x_in.size())
        reg = grad_dout2.sum()/batch_size
        return reg

    def calc_wasserstein_loss(self, pred_real, pred_fake):
        print("pred_real: shape: ",pred_real.shape," dtype: ",pred_real.dtype)
        print("pred_fake: shape: ",pred_fake.shape," dtype: ",pred_fake.dtype)
        if(pred_fake.dtype != pred_real.dtype):
            print("In Wasserstein, pred_real and pred_fake didn't have same datatype. Casting",pred_fake.dtype," to",pred_real.dtype)
            pred_fake = pred_fake.type(torch.float32)
            
        loss_D_real = torch.mean(pred_real) - torch.mean(pred_fake)
        return loss_D_real


class FewShotGen(nn.Module):
    def __init__(self, hp):
        super(FewShotGen, self).__init__()
        nf = hp['nf']
        nf_mlp = hp['nf_mlp']
        down_class = hp['n_downs_class']
        down_content = hp['n_downs_content']
        n_mlp_blks = hp['n_mlp_blks']
        n_res_blks = hp['n_res_blks']
        latent_dim = hp['latent_dim']
        input_channels = hp['input_nc']
        output_channels = hp['output_nc']
        self.enc_class_model = ClassModelEncoder(down_class,
                                                 input_channels,
                                                 nf,
                                                 latent_dim,
                                                 norm='none',
                                                 activ='relu',
                                                 pad_type='reflect')

        self.enc_content = ContentEncoder(down_content,
                                          n_res_blks,
                                          input_channels,
                                          nf,
                                          'in',
                                          activ='relu',
                                          pad_type='reflect')

        self.dec = Decoder(down_content,
                           n_res_blks,
                           self.enc_content.output_dim,
                           output_channels,
                           res_norm='adain',
                           activ='relu',
                           pad_type='reflect')

        self.mlp = MLP(latent_dim,
                       get_num_adain_params(self.dec),
                       nf_mlp,
                       n_mlp_blks,
                       norm='none',
                       activ='relu')

    def forward(self, one_image, model_set):
        # reconstruct an image
        content, model_codes = self.encode(one_image, model_set)
        model_code = torch.mean(model_codes, dim=0).unsqueeze(0)
        images_trans = self.decode(content, model_code)
        return images_trans

    def encode(self, one_image, model_set):
        # extract content code from the input image
        content = self.enc_content(one_image)
        # extract model code from the images in the model set
        class_codes = self.enc_class_model(model_set)
        class_code = torch.mean(class_codes, dim=0).unsqueeze(0)
        return content, class_code

    def decode(self, content, model_code):
        # decode content and style codes to an image
        adain_params = self.mlp(model_code)
        assign_adain_params(adain_params, self.dec)
        images = self.dec(content)
        return images


class ClassModelEncoder(nn.Module):
    def __init__(self, downs, ind_im, dim, latent_dim, norm, activ, pad_type):
        super(ClassModelEncoder, self).__init__()
        #InceptionBlock = Conv2dBlock
        self.model = []
        self.model += [InceptionBlock(ind_im, dim, KERNEL_SIZE_7, 1, 3,
                                   norm=norm,
                                   activation=activ,
                                   pad_type=pad_type)]
        for i in range(2):
            self.model += [InceptionBlock(dim, 2 * dim, KERNEL_SIZE_4, 2, 1,
                                       norm=norm,
                                       activation=activ,
                                       pad_type=pad_type)]
            dim *= 2
        for i in range(downs - 2):
            self.model += [Conv2dBlock(dim, dim, KERNEL_SIZE_4, 2, 1,
                                       norm=norm,
                                       activation=activ,
                                       pad_type=pad_type)]
        self.model += [nn.AdaptiveAvgPool2d(1)]
        self.model += [nn.Conv2d(dim, latent_dim, 1, 1, 0)]
        self.model = nn.Sequential(*self.model)
        self.output_dim = dim

        #self.register_backward_hook(debug.printgradnorm)

    def forward(self, x):
        #print("FORWARD ",self.__class__.__name__)
        return self.model(x)


class ContentEncoder(nn.Module):
    def __init__(self, downs, n_res, input_dim, dim, norm, activ, pad_type):
        super(ContentEncoder, self).__init__()
        #InceptionBlock = Conv2dBlock
        self.model = []
        self.model += [InceptionBlock(input_dim, dim, KERNEL_SIZE_7, 1, 3,
                                   norm=norm,
                                   activation=activ,
                                   pad_type=pad_type)]
        
        """
        for i in range(downs):
            self.model += [InceptionBlock(dim, 2 * dim, KERNEL_SIZE_4, 1,#2, 
                                        1,
                                       norm=norm,
                                       activation=activ,
                                       pad_type=pad_type)]
            if (i == downs-1):
                self.model += [
                    nn.MaxPool2d(KERNEL_SIZE_4, 2, padding=1)
                ]
            else:
                self.model += [
                    nn.MaxPool2d(KERNEL_SIZE_4, 2, padding=0)
                ]
            dim *= 2       
        """
        for i in range(downs):
            self.model += [Conv2dBlock(dim, 2 * dim, KERNEL_SIZE_4, 2, 1,
                                       norm=norm,
                                       activation=activ,
                                       pad_type=pad_type)]
            dim *= 2 
        self.model += [ResBlocks(n_res, dim,
                                 norm=norm,
                                 activation=activ,
                                 pad_type=pad_type,
                                 inception=True)]
        self.model = nn.Sequential(*self.model)
        self.output_dim = dim

        #self.register_backward_hook(debug.printgradnorm)

    def forward(self, x):
        #print("FORWARD ",self.__class__.__name__)
        return self.model(x)


class Decoder(nn.Module):
    def __init__(self, ups, n_res, dim, out_dim, res_norm, activ, pad_type):
        super(Decoder, self).__init__()
        #InceptionBlock = Conv2dBlock
        self.model = []
        self.model += [ResBlocks(n_res, dim, res_norm,
                                 activ, pad_type=pad_type,
                                 inception=True)]
        for i in range(ups):
            self.model += [nn.Upsample(scale_factor=2),
                           InceptionBlock(dim, dim // 2, KERNEL_SIZE_5, 1, 2,
                                       norm='in',
                                       activation=activ,
                                       pad_type=pad_type)]
            dim //= 2
        self.model += [Conv2dBlock(dim, out_dim, KERNEL_SIZE_7, 1, 3,
                                   norm='none',
                                   activation='tanh',
                                   pad_type=pad_type)]

        self.model = nn.Sequential(*self.model)

        #self.register_backward_hook(debug.printgradnorm)



    def forward(self, x):
        #print("FORWARD ",self.__class__.__name__)
        return self.model(x)


class MLP(nn.Module):
    def __init__(self, in_dim, out_dim, dim, n_blk, norm, activ):

        super(MLP, self).__init__()
        self.model = []
        self.model += [LinearBlock(in_dim, dim, norm=norm, activation=activ)]
        for i in range(n_blk - 2):
            self.model += [LinearBlock(dim, dim, norm=norm, activation=activ)]
        self.model += [LinearBlock(dim, out_dim,
                                   norm='none', activation='none')]
        self.model = nn.Sequential(*self.model)

        #self.register_backward_hook(debug.printgradnorm)

    def forward(self, x):
        #print("FORWARD ",self.__class__.__name__)
        return self.model(x.view(x.size(0), -1))
