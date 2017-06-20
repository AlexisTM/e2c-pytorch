import argparse
import matplotlib
# matplotlib.use('qt5')
import matplotlib.pyplot as plt
import numpy as np
import os
import sys

from pixel2torque.pytorch.e2c import E2C
from pixel2torque.pytorch.vae import VAE
import torch
from torch import optim
from torch.autograd import Variable
from torch.utils.data import DataLoader
from pixel2torque.pytorch.datasets import GymPendulumDataset

USE_CUDA = torch.cuda.is_available()


def parse_args():
    parser = argparse.ArgumentParser(description='train e2c on plane data')
    parser.add_argument('--batch-size', required=False, default=128, type=int)
    parser.add_argument('--lr', required=False, default=3e-4, type=float)
    parser.add_argument('--seed', required=False, default=1234, type=int)
    parser.add_argument('--model-dir', required=True)
    parser.add_argument('--data', required=True)
    parser.add_argument('--test-frequency', default=100)
    parser.add_argument('--eval-frequency', default=1000)
    parser.add_argument('--visual', action='store_true')
    parser.add_argument('--epochs', default=100000)
    return parser.parse_args()


def weights_init(m):
    from torch.nn.init import xavier_uniform, orthogonal
    if isinstance(m, torch.nn.Linear):
        xavier_uniform(m.weight.data)


def make_model_dir(basedir, params):
    hyperparams = ['lr']
    return os.path.join(basedir, *['-'.join([p, str(params[p])]) for p in hyperparams])


def checkpoint(epoch, model, model_dir):
    path = os.path.join(model_dir, 'model.pth-{}'.format(epoch))
    torch.save(model, path)
    print('checkpoint saved to {}'.format(path))


if __name__ == '__main__':
    args = parse_args()

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    width = 40
    height = width
    model_dir = make_model_dir(args.model_dir, {'lr': args.lr})

    if not os.path.exists(model_dir):
        os.makedirs(model_dir, exist_ok=False)

    dataset = GymPendulumDataset(args.data)

    all_states = Variable(torch.from_numpy(GymPendulumDataset.all_states())
                          .float().view(360, width * height),
                          requires_grad=False)

    batch_size = 128

    print('batch_size %d' % batch_size)

    loader = DataLoader(dataset, batch_size, shuffle=False,
                        drop_last=True)

    input_dim = width * height
    latent_dim = 3
    action_dim = 1

    model = VAE(input_dim, latent_dim, config='pendulum')

    print(model)
    weights_init(model)

    optimizer = optim.Adam(model.parameters(), lr=args.lr,
                           betas=(0.1, 0.1))

    fig = plt.figure("Embeddings")


    def viz_embedding(model):
        # Visualize a 3d embedding
        fig.clear()
        ax = fig.add_subplot(111, projection='3d')
        embeds = model.latent_embeddings(all_states).data.cpu().numpy()
        ax.scatter(embeds[:, 0], embeds[:, 1], embeds[:, 2])
        plt.pause(0.001)


    step = 0
    for epoch in range(args.epochs):
        for i, (x, u, x_next) in enumerate(loader):
            x = Variable(
                x.resize_(batch_size, input_dim).float(), requires_grad=False)
            u = Variable(u.float(), requires_grad=False)
            x_next = Variable(x_next.resize_(batch_size, input_dim).float(),
                              requires_grad=False)
            if USE_CUDA:
                x.data.cuda()
                u.data.cuda()
                x_next.cuda()

            optimizer.zero_grad()

            dec, closure = model(x, u, x_next)
            reconst_loss, latent_loss = closure()
            loss = reconst_loss.add(latent_loss)

            loss.backward()
            optimizer.step()

            aggregate_loss = loss.data[0]

            if step >= args.epochs:
                sys.exit(0)

            if step % 1000 == 0:
                checkpoint(step, model, model_dir)

            if step % 1000 == 0:
                viz_embedding(model)
                plt.savefig(os.path.join(model_dir, 'embeddings_step-{:05d}'.format(step)))

            if step % 100 == 0:
                print('step: {}, loss: {}, bound_loss: {}, kl_loss: {}' \
                      .format(step, aggregate_loss, reconst_loss.data[0], latent_loss.data[0]))

            step += 1
