#!/usr/bin/env python
"""
Implementation of Embed-to-Control model: http://arxiv.org/abs/1506.07365
Code is organized for simplicity and readability w.r.t paper.

Author: Eric Jang
"""

import numpy as np
import tensorflow as tf
import matplotlib.pyplot as plt
import os
from data.plane_data2 import PlaneData, get_params

import ipdb as pdb
# np.random.seed(0)
tf.set_random_seed(0)

A = B = 40

x_dim, u_dim, T = get_params()
z_dim = 2  # latent space dimensionality
eps = 1e-9  # numerical stability


def orthogonal_initializer(scale=1.1):
    """
  From Lasagne and Keras. Reference: Saxe et al., http://arxiv.org/abs/1312.6120
  """

    def _initializer(shape, dtype=tf.float32):
        flat_shape = (shape[0], np.prod(shape[1:]))
        a = np.random.normal(0.0, 1.0, flat_shape)
        u, _, v = np.linalg.svd(a, full_matrices=False)
        # pick the one with the correct shape
        q = u if u.shape == flat_shape else v
        q = q.reshape(shape)
        print(
            'Warning -- You have opted to use the orthogonal_initializer function'
        )
        return tf.constant(scale * q[:shape[0], :shape[1]], dtype=tf.float32)

    return _initializer


class NormalDistribution(object):
    """
  Represents a multivariate normal distribution parameterized by
  N(mu,Cov). If cov. matrix is diagonal, Cov=(sigma).^2. Otherwise,
  Cov=A*(sigma).^2*A', where A = (I+v*r^T).
  """

    def __init__(self, mu, sigma, logsigma, v=None, r=None):
        self.mu = mu
        self.sigma = sigma  # either stdev diagonal itself, or stdev diagonal from decomposition
        self.logsigma = logsigma
        dim = mu.get_shape()
        if v is None:
            v = tf.constant(0., shape=dim)
        if r is None:
            r = tf.constant(0., shape=dim)
        self.v = v
        self.r = r


def linear(x, output_dim):
    w = tf.get_variable(
        "w", [x.get_shape()[1], output_dim],
        initializer=orthogonal_initializer(1.1))
    b = tf.get_variable(
        "b", [output_dim], initializer=tf.constant_initializer(0.0))
    return tf.matmul(x, w) + b


def ReLU(x, output_dim, scope):
    # helper function for implementing stacked ReLU layers
    with tf.variable_scope(scope):
        return tf.nn.relu(linear(x, output_dim))


def encode(x, share=None):
    with tf.variable_scope("encoder", reuse=share):
        for l in range(3):
            x = ReLU(x, 150, "aggregate_loss" + str(l))
        return linear(x, 2 * z_dim)


def KLGaussian(Q, N):
    # Q, N are instances of NormalDistribution
    # implements KL Divergence term KL(N0,N1) derived in Appendix A.1
    # Q ~ Normal(mu,A*sigma*A^T), N ~ Normal(mu,sigma_1)
    # returns scalar divergence, measured in nats (information units under log rather than log2), shape= batch x 1
    sum = lambda x: tf.reduce_sum(x, 1)  # convenience fn for summing over features (columns)
    k = float(Q.mu.get_shape()[1].value)  # dimension of distribution
    mu0, v, r, mu1 = Q.mu, Q.v, Q.r, N.mu
    s02, s12 = tf.square(Q.sigma), tf.square(N.sigma) + eps
    #vr=sum(v*r)
    a = sum(s02 * (1. + 2. * v * r) / s12) + sum(tf.square(v) / s12) * sum(
        tf.square(r) * s02)  # trace term
    b = sum(tf.square(mu1 - mu0) / s12)  # difference-of-means term
    c = 2. * (sum(N.logsigma - Q.logsigma) - tf.log(1. + sum(v * r))
              )  # ratio-of-determinants term. 
    return 0.5 * (a + b - k + c)  #, a, b, c


def sampleNormal(mu, sigma):
    # diagonal stdev
    n01 = tf.random_normal(sigma.get_shape(), mean=0, stddev=1)
    return mu + sigma * n01


def sampleQ_phi(h_enc, share=None):
    with tf.variable_scope("sampleQ_phi", reuse=share):
        mu, log_sigma = tf.split(1, 2, linear(
            h_enc, z_dim * 2))  # diagonal stdev values
        sigma = tf.exp(log_sigma)
        return sampleNormal(mu, sigma), NormalDistribution(
            mu, sigma, log_sigma)


def transition(h):
    # compute A,B,o linearization matrices
    with tf.variable_scope("trans"):
        for l in range(2):
            h = ReLU(h, 100, "aggregate_loss" + str(l))
        with tf.variable_scope("A"):
            v, r = tf.split(1, 2, linear(h, z_dim * 2))
            v1 = tf.expand_dims(v, -1)  # (batch, z_dim, 1)
            rT = tf.expand_dims(r, 1)  # batch, 1, z_dim
            I = tf.diag([1.] * z_dim)
            A = (
                I + tf.batch_matmul(v1, rT)
            )  # (z_dim, z_dim) + (batch, z_dim, 1)*(batch, 1, z_dim) (I is broadcasted) 
        with tf.variable_scope("B"):
            B = linear(h, z_dim * u_dim)
            B = tf.reshape(B, [-1, z_dim, u_dim])
        with tf.variable_scope("o"):
            o = linear(h, z_dim)
        return A, B, o, v, r


def sampleQ_psi(z, u, Q_phi):
    A, B, o, v, r = transition(z)
    with tf.variable_scope("sampleQ_psi"):
        mu_t = tf.expand_dims(Q_phi.mu, -1)  # batch,z_dim,1
        Amu = tf.squeeze(tf.batch_matmul(A, mu_t), [-1])
        u = tf.expand_dims(u, -1)  # batch,u_dim,1
        Bu = tf.squeeze(tf.batch_matmul(B, u), [-1])
        Q_psi = NormalDistribution(Amu + Bu + o, Q_phi.sigma, Q_phi.logsigma,
                                   v, r)
        # the actual z_next sample is generated by deterministically transforming z_t
        z = tf.expand_dims(z, -1)
        Az = tf.squeeze(tf.batch_matmul(A, z), [-1])
        z_next = Az + Bu + o
        return z_next, Q_psi  #,(A,B,o,v,r) # debugging


def decode(z, share=None):
    with tf.variable_scope("decoder", reuse=share):
        for l in range(2):
            z = ReLU(z, 200, "aggregate_loss" + str(l))
        return linear(z, x_dim)


def binary_crossentropy(t, o):
    return t * tf.log(o + eps) + (1.0 - t) * tf.log(1.0 - o + eps)


def recons_loss(x, x_recons):
    with tf.variable_scope("Lx"):
        ll = tf.reduce_sum(binary_crossentropy(x, x_recons),
                           1)  # sum across features
        return -ll  # negative log-likelihood


def latent_loss(Q):
    with tf.variable_scope("Lz"):
        mu2 = tf.square(Q.mu)
        sigma2 = tf.square(Q.sigma)
        # negative of the upper bound of posterior
        return -0.5 * tf.reduce_sum(1 + 2 * Q.logsigma - mu2 - sigma2, 1)


def sampleP_theta(h_dec, share=None):
    # sample x from bernoulli distribution with means p=W(h_dec)
    with tf.variable_scope("P_theta", reuse=share):
        p = linear(h_dec, x_dim)
        return tf.sigmoid(p)  # mean of bernoulli distribution


# BUILD NETWORK
batch_size = 128

x = tf.placeholder(tf.float32, [batch_size, x_dim])
u = tf.placeholder(tf.float32, [batch_size, u_dim])  # control at time t
x_next = tf.placeholder(tf.float32, [batch_size,
                                     x_dim])  # observation at time t+1

# encode x_t
h_enc = encode(x)
z, Q_phi = sampleQ_phi(h_enc)
# reconstitute x_t
h_dec = decode(z)
x_recons = sampleP_theta(h_dec)
# compute linearized dynamics, predict new latent state
z_predict, Q_psi = sampleQ_psi(z, u, Q_phi)
# decode prediction
h_dec_predict = decode(z_predict, share=True)
x_predict = sampleP_theta(h_dec_predict, share=True)
# encode next 
h_enc_next = encode(x_next, share=True)
z_next, Q_phi_next = sampleQ_phi(h_enc_next, share=True)

with tf.variable_scope("Loss"):
    L_x = recons_loss(x, x_recons)
    L_x_next = recons_loss(x_next, x_predict)
    L_z = latent_loss(Q_phi)
    L_bound = L_x + L_x_next + L_z
    KL = KLGaussian(Q_psi, Q_phi_next)
    lambd = 0.25
    loss = tf.reduce_mean(
        L_bound + lambd * KL)  # average loss over minibatch to single scalar

for v in tf.all_variables():
    print("%s : %s" % (v.name, v.get_shape()))

pdb.set_trace()

with tf.variable_scope("Optimizer"):
    learning_rate = 1e-4
    optimizer = tf.train.AdamOptimizer(
        learning_rate, beta1=0.1, beta2=0.1)  # beta2=0.1
    train_op = optimizer.minimize(loss)

saver = tf.train.Saver(max_to_keep=200)  # keep all checkpoint files

ckpt_file = "/ltmp/e2c-plane"

# summaries
tf.scalar_summary("loss", loss)
tf.scalar_summary("L_x", tf.reduce_mean(L_x))
tf.scalar_summary("L_x_next", tf.reduce_mean(L_x_next))
tf.scalar_summary("L_z", tf.reduce_mean(L_z))
tf.scalar_summary("KL", tf.reduce_mean(KL))
all_summaries = tf.merge_all_summaries()

# TRAIN
if __name__ == "__main__":
    init = tf.initialize_all_variables()
    sess = tf.InteractiveSession()
    sess.run(init)
    # WRITER
    writer = tf.train.SummaryWriter("/ltmp/e2c", sess.graph_def)

    dataset = PlaneData("data/plane1.npz", "data/env1.png")
    dataset.initialize()

    # tmp
    # (x_val,u_val,x_next_val)=dataset.sample(batch_size, replace=False)
    # feed_dict={
    #   x:x_val,
    #   u:u_val,
    #   x_next:x_next_val
    # }
    # results=sess.run([L_x,L_x_next,L_z,L_bound,KL],feed_dict)
    # pdb.set_trace()
    # resume training
    #saver.restore(sess, "/ltmp/e2c-plane-83000.ckpt")
    train_iters = 2e5  # 5K iters
    for i in range(int(train_iters)):
        (x_val, u_val, x_next_val) = dataset.sample(batch_size, replace=False)
        feed_dict = {x: x_val, u: u_val, x_next: x_next_val}
        plt.hist(x_val[0, :])
        plt.show()
        results = sess.run([loss, all_summaries, train_op], feed_dict)
        if i % 1000 == 0:
            print("iter=%d : Loss: %f" % (i, results[0]))
            if i > 2000:
                writer.add_summary(results[1], i)
        if (i % 100 == 0 and i < 1000) or (i % 1000 == 0):
            saver.save(sess, ckpt_file + "-%05d" % (i) + ".ckpt")

    sess.close()