"""
Copyright (C) 2019 NVIDIA Corporation.  All rights reserved.
Licensed under the CC BY-NC-SA 4.0 license
(https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode).
"""
import copy

import torch
import torch.nn as nn

from networks import FewShotGen, GPPatchMcResDis
from debugUtils import printCheckpoint, Debugger
import torch.nn.functional as F
from globalConstants import GlobalConstants

import apex.amp as amp

import customLosses

PREFIX = "funit_model.py"

def recon_criterion(predict, target):
    if (target.shape[-1]!=predict.shape[-1]):
        print("Funit_model.py recon_criterion: SHAPE OF INPUT", target.shape, "AND OF PREDICTION", predict.shape, "AREN'T EQUAL!")
        predict = F.interpolate(predict, target.shape[-1])
    return torch.mean(torch.abs(predict - target))


class FUNITModel(nn.Module):
    def __init__(self, hp):
        super(FUNITModel, self).__init__()
        self.gen = FewShotGen(hp['gen'])
        self.dis = GPPatchMcResDis(hp['dis'])
        self.gen_test = copy.deepcopy(self.gen)

    #content: co    -> Gives the positions  (image to be transformed from)
    #class: cl      -> Gives the texture    (image to be transformed into)
    def forward(self, co_data, cl_data, hp, mode, it):
    
        #debug = Debugger(self.forward.__name__, self.__class__.__name__, PREFIX) #Delete afterwards

        xa = co_data[0].cuda()
        label_a = co_data[1].cuda()
        xb = cl_data[0].cuda()
        label_b = cl_data[1].cuda()
        if mode == 'gen_update':
            c_xa = self.gen.enc_content(xa)
            s_xa = self.gen.enc_class_model(xa)
            s_xb = self.gen.enc_class_model(xb)
            x_translation = self.gen.decode(c_xa, s_xb)  # translation
            xa_reconstruct = self.gen.decode(c_xa, s_xa)  # reconstruction
            #GAN-Gen-Loss
            #ADVESERIAL LOSS <---- Wasserstein
            l_adv_t, gen_acc_translation, xt_gan_feat = self.dis.calc_gen_loss(x_translation, label_b)
            l_adv_r, gen_acc_reconstruct, xr_gan_feat = self.dis.calc_gen_loss(xa_reconstruct, label_a)
            _, xb_gan_feat = self.dis(xb, label_b)
            _, xa_gan_feat = self.dis(xa, label_a)
            #l_c_reconst = loss content reconstruct??
            l_c_rec = recon_criterion(xr_gan_feat.mean(3).mean(2),
                                      xa_gan_feat.mean(3).mean(2))
            l_m_rec = recon_criterion(xt_gan_feat.mean(3).mean(2),
                                      xb_gan_feat.mean(3).mean(2))
            l_x_rec = recon_criterion(xa_reconstruct, xa.float())
            l_adv = 0.5 * (l_adv_t + l_adv_r)
            acc = 0.5 * (gen_acc_translation + gen_acc_reconstruct)
            #We only want every tenth iteration loss adversary loss
            if (it%hp['gen']['update_every']==0):
                l_total = (hp['gan_w'] * l_adv + hp['r_w'] * l_x_rec + hp['fm_w'] * (l_c_rec + l_m_rec))
            else:
                l_total =  (hp['r_w'] * l_x_rec + hp['fm_w'] * (l_c_rec + l_m_rec))
            if (GlobalConstants.usingApex):
                with amp.scale_loss(l_total, [self.gen_opt, self.dis_opt]) as scaled_loss:
                    scaled_loss.backward()
            else:
                l_total.backward()
            return l_total, l_adv, l_x_rec, l_c_rec, l_m_rec, acc
        elif mode == 'dis_update':
            xb.requires_grad_()
            #resp_r is exactly the output of the discrimator which classifies how likely it
            #thinks the output is real
            l_real_pre, acc_r, resp_r = self.dis.calc_dis_real_loss(xb, label_b)

            with torch.no_grad():
                c_xa = self.gen.enc_content(xa)
                s_xb = self.gen.enc_class_model(xb)
                x_translation = self.gen.decode(c_xa, s_xb)
            l_fake_p, acc_f, resp_f = self.dis.calc_dis_fake_loss(x_translation.detach(),
                                                                  label_b)

            #===Wasserstein Loss======#
            loss_D_real = self.dis.calc_wasserstein_loss(resp_r, resp_f)
            penalty = customLosses.gradient_penalty_FUNIT(xb, x_translation, self.dis, label_b, 10)
            loss_wasserstein = loss_D_real + penalty
            if (GlobalConstants.usingApex):
                with amp.scale_loss(loss_wasserstein, [self.gen_opt, self.dis_opt]) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss_wasserstein.backward()

            l_total = loss_wasserstein
            acc = 0.5 * (acc_f + acc_r)
            return l_total, l_fake_p, l_real_pre, acc
        else:
            assert 0, 'Not support operation'

    def test(self, co_data, cl_data):
        self.eval()
        self.gen.eval()
        self.gen_test.eval()
        xa = co_data[0].cuda()
        xb = cl_data[0].cuda()
        c_xa_current = self.gen.enc_content(xa)
        s_xa_current = self.gen.enc_class_model(xa)
        s_xb_current = self.gen.enc_class_model(xb)
        xt_current = self.gen.decode(c_xa_current, s_xb_current)
        xr_current = self.gen.decode(c_xa_current, s_xa_current)
        c_xa = self.gen_test.enc_content(xa)
        s_xa = self.gen_test.enc_class_model(xa)
        s_xb = self.gen_test.enc_class_model(xb)
        x_translation = self.gen_test.decode(c_xa, s_xb)
        xr = self.gen_test.decode(c_xa, s_xa)
        self.train()
        return xa, xr_current, xt_current, xb, xr, x_translation

    def translate_k_shot(self, co_data, cl_data, k):
        self.eval()
        xa = co_data[0].cuda()
        xb = cl_data[0].cuda()
        c_xa_current = self.gen_test.enc_content(xa)
        if k == 1:
            c_xa_current = self.gen_test.enc_content(xa)
            s_xb_current = self.gen_test.enc_class_model(xb)
            xt_current = self.gen_test.decode(c_xa_current, s_xb_current)
        else:
            s_xb_current_before = self.gen_test.enc_class_model(xb)
            s_xb_current_after = s_xb_current_before.squeeze(-1).permute(1,
                                                                         2,
                                                                         0)
            s_xb_current_pool = torch.nn.functional.avg_pool1d(
                s_xb_current_after, k)
            s_xb_current = s_xb_current_pool.permute(2, 0, 1).unsqueeze(-1)
            xt_current = self.gen_test.decode(c_xa_current, s_xb_current)
        return xt_current

    def compute_k_style(self, style_batch, k):
        self.eval()
        style_batch = style_batch.cuda()
        s_xb_before = self.gen_test.enc_class_model(style_batch)
        s_xb_after = s_xb_before.squeeze(-1).permute(1, 2, 0)
        s_xb_pool = torch.nn.functional.avg_pool1d(s_xb_after, k)
        s_xb = s_xb_pool.permute(2, 0, 1).unsqueeze(-1)
        return s_xb

    def translate_simple(self, content_image, class_code):
        self.eval()
        xa = content_image.cuda()
        s_xb_current = class_code.cuda()
        c_xa_current = self.gen_test.enc_content(xa)
        xt_current = self.gen_test.decode(c_xa_current, s_xb_current)
        return xt_current

    def setOptimizersForApex(self, gen_opt, dis_opt):
        self.gen_opt = gen_opt
        self.dis_opt = dis_opt
