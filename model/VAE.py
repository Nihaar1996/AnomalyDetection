# Aspects of code borrowed from github.com/rtqichen/ffjord/

import torch
import torch.nn as nn
import torch.nn.functional as F

import model.flows as flows


class VAE(nn.Module):
    def __init__(
        self,
        input_size,
        encoder_sizes,
        decoder_sizes,
        z_size,
        batch_norm=False,
        dropout=0,
        num_flows=4,
        made_h_size=320,
    ):
        super().__init__()
        self.input_size = input_size
        self.z_size = z_size
        self.encoder_sizes = [input_size] + encoder_sizes
        self.decoder_sizes = [self.z_size] + decoder_sizes
        self.batch_norm = batch_norm
        self.dropout = dropout
        self._set_encoder()
        self._set_decoder()
        self.num_flows = num_flows
        self.made_h_size = made_h_size
        self.q_z_nn_output_dim = encoder_sizes[-1]  # the size of the bottleneck

        self.FloatTensor = (
            torch.FloatTensor if not torch.cuda.is_available() else torch.cuda.FloatTensor
        )

        # log-det-jacobian = 0 without flows
        self.log_det_j = self.FloatTensor(1).zero_()

    def _set_encoder(self):
        """Set encoder layers and z_mu, z_var """

        layers = []
        for l in range(1, len(self.encoder_sizes)):
            layers.append(nn.Linear(self.encoder_sizes[l - 1], self.encoder_sizes[l]))
            layers.append(nn.LeakyReLU(0.1))
            if self.batch_norm:
                layers.append(nn.BatchNorm1d(self.encoder_sizes[l]))
            layers.append(nn.Dropout(self.dropout))

        self.encoder = nn.Sequential(*layers)
        self.z_mu = nn.Linear(self.encoder_sizes[-1], self.z_size)
        self.z_var = nn.Sequential(nn.Linear(self.encoder_sizes[-1], self.z_size), nn.Softplus())

    def _set_decoder(self):
        """Set decoder layers """

        layers = []
        for l in range(1, len(self.decoder_sizes)):
            layers.append(nn.Linear(self.decoder_sizes[l - 1], self.decoder_sizes[l]))
            layers.append(nn.LeakyReLU(0.1))
            if self.batch_norm:
                layers.append(nn.BatchNorm1d(self.decoder_sizes[l]))
            layers.append(nn.Dropout(self.dropout))
        layers.append(nn.Linear(self.decoder_sizes[-1], self.input_size))
        self.decoder = nn.Sequential(*layers)

    def reparameterize(self, mu, var):
        """Reparameterization trick (sample z via a standard normal)"""

        std = var.sqrt()
        eps = self.FloatTensor(std.size()).normal_()
        z = eps.mul(std).add_(mu)

        return z

    def encode(self, x):

        x = self.encoder(x)
        mu = self.z_mu(x)
        var = self.z_var(x)

        return mu, var

    def decode(self, z):

        return self.decoder(z)

    def forward(self, x):

        z_mu, z_var = self.encode(x)
        z = self.reparameterize(z_mu, z_var)

        # Normalizing flows here

        output = self.decode(z)

        return output, z_mu, z_var, self.log_det_j, z, z


class PlanarVAE(VAE):
    """
    Adopted from https://github.com/rtqichen/ffjord
    Variational auto-encoder with planar flows in the encoder.
    """

    def __init__(self, *args, **kwargs):
        super(PlanarVAE, self).__init__(*args, **kwargs)

        # Initialize log-det-jacobian to zero
        self.log_det_j = 0.0

        # Flow parameters
        flow = flows.Planar

        # Amortized flow parameters
        self.amor_u = nn.Linear(self.q_z_nn_output_dim, self.num_flows * self.z_size)
        self.amor_w = nn.Linear(self.q_z_nn_output_dim, self.num_flows * self.z_size)
        self.amor_b = nn.Linear(self.q_z_nn_output_dim, self.num_flows)

        # Normalizing flow layers
        for k in range(self.num_flows):
            flow_k = flow()
            self.add_module("flow_" + str(k), flow_k)

    def encode(self, x):
        """
        Encoder that ouputs parameters for base distribution of z and flow parameters.
        """

        batch_size = x.size(0)

        h = self.encoder(x)
        h = h.view(-1, self.q_z_nn_output_dim)
        # print("hidden unit has shape",h.shape) # 250 x 256
        mean_z = self.z_mu(h)
        var_z = self.z_var(h)

        # return amortized u an w for all flows
        u = self.amor_u(h).view(batch_size, self.num_flows, self.z_size, 1)
        w = self.amor_w(h).view(batch_size, self.num_flows, 1, self.z_size)
        b = self.amor_b(h).view(batch_size, self.num_flows, 1, 1)

        return mean_z, var_z, u, w, b

    def forward(self, x):
        """
        Forward pass with planar flows for the transformation z_0 -> z_1 -> ... -> z_k.
        Log determinant is computed as log_det_j = N E_q_z0[\sum_k log |det dz_k/dz_k-1| ].
        """

        self.log_det_j = 0.0

        z_mu, z_var, u, w, b = self.encode(x)

        # Sample z_0
        z = [self.reparameterize(z_mu, z_var)]

        # Normalizing flows
        for k in range(self.num_flows):
            flow_k = getattr(self, "flow_" + str(k))
            z_k, log_det_jacobian = flow_k(z[k], u[:, k, :, :], w[:, k, :, :], b[:, k, :, :])
            z.append(z_k)
            self.log_det_j += log_det_jacobian

        x_mean = self.decode(z[-1])

        return x_mean, z_mu, z_var, self.log_det_j, z[0], z[-1]


class IAFVAE(VAE):
    """
    Adopted from https://github.com/rtqichen/ffjord
    Variational auto-encoder with inverse autoregressive flows in the encoder.
    """

    def __init__(self, *args, **kwargs):
        super(IAFVAE, self).__init__(*args, **kwargs)

        # Initialize log-det-jacobian to zero
        self.log_det_j = 0.0
        self.h_size = self.made_h_size

        self.h_context = nn.Linear(self.q_z_nn_output_dim, self.h_size)

        # Flow parameters
        self.flow = flows.IAF(
            z_size=self.z_size,
            num_flows=self.num_flows,
            num_hidden=1,
            h_size=self.h_size,
            conv2d=False,
        )

    def encode(self, x):
        """
        Encoder that ouputs parameters for base distribution of z and context h for flows.
        """

        h = self.encoder(x)
        h = h.view(-1, self.q_z_nn_output_dim)
        mean_z = self.z_mu(h)
        var_z = self.z_var(h)
        h_context = self.h_context(h)

        return mean_z, var_z, h_context

    def forward(self, x):
        """
        Forward pass with inverse autoregressive flows for the transformation z_0 -> z_1 -> ... -> z_k.
        Log determinant is computed as log_det_j = N E_q_z0[\sum_k log |det dz_k/dz_k-1| ].
        """

        # mean and variance of z
        z_mu, z_var, h_context = self.encode(x)
        # sample z
        z_0 = self.reparameterize(z_mu, z_var)

        # iaf flows
        z_k, self.log_det_j = self.flow(z_0, h_context)

        # decode
        x_mean = self.decode(z_k)

        return x_mean, z_mu, z_var, self.log_det_j, z_0, z_k
