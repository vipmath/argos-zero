import capnp
import h5py
import sys
import glob
import os
import numpy as np
sys.path.append('/Users/valentinwolf/Documents/Studium/Machine_Learning/SoftwareProjekt')
import CapnpGame_capnp
import mxnet as mx
from mxnet import nd, autograd, gluon
import sys
import parse_data

#data = h5py.File('/home/julianstastny/Documents/Softwareprojekt/argos-zero/python/train_val.h5','r')
raw_data_folder = '/Users/valentinwolf/Documents/Studium/Machine_Learning/SoftwareProjekt/raw_data/*'
dataset_path = '/Users/valentinwolf/Documents/Studium/Machine_Learning/SoftwareProjekt/train_val.h5'

parse_data.update_dataset(raw_data_folder,dataset_path)

data = h5py.File(dataset_path)
print(list(data.keys()))
ctx = mx.cpu()
batch_size = 128

def update_data():
    data.close()
    parse_data.update_dataset(raw_data_folder,dataset_path)
    data = h5py.File(dataset_path)
    inputs_t = data['train_x']
    labels_t = data['train_y']
    inputs_v = data['val_x']
    labels_v = data['val_y']

inputs_t = data['train_x']
labels_t = data['train_y']
inputs_v = data['val_x']
labels_v = data['val_y']

diter = mx.io.NDArrayIter(inputs_t, labels_t, batch_size=batch_size, last_batch_handle='roll_over')
val_set_x = mx.nd.array(inputs_v).as_in_context(mx.cpu())
val_set_y = mx.nd.array(labels_v).as_in_context(mx.cpu())

def _conv3x3(channels, stride, in_channels, kernel_size, groups, padding):
    return gluon.nn.Conv2D(channels, kernel_size=kernel_size, strides=stride, padding=padding,
                     use_bias=False, in_channels=in_channels, groups=groups)

class BasicBlockV2(gluon.HybridBlock):
    def __init__(self, channels, stride=1, in_channels=0, **kwargs):
        super(BasicBlockV2, self).__init__(**kwargs)
        self.convs = gluon.nn.HybridSequential()
        self.convs.add(gluon.nn.BatchNorm())
        self.convs.add(gluon.nn.LeakyReLU(alpha=0.3))
        self.convs.add(_conv3x3(channels, stride, in_channels, 3, 1, 1))
        self.convs.add(gluon.nn.BatchNorm())
        self.convs.add(gluon.nn.LeakyReLU(alpha=0.3))
        self.convs.add(_conv3x3(channels, 1, channels, 3, 1, 1))

    def hybrid_forward(self, F, x):
        residual = x
        x = self.convs(x)
        return x + residual

class CombinedNet(gluon.HybridBlock):
    def __init__(self, num_filters, num_blocks, **kwargs):
        super(CombinedNet, self).__init__(**kwargs)

        with self.name_scope():
            self.convs = gluon.nn.HybridSequential()
            self.convs.add(gluon.nn.Conv2D(num_filters, 3, padding=1))
            self.convs.add(gluon.nn.LeakyReLU(alpha=0.3))

            for _ in range(num_blocks):
                self.convs.add(BasicBlockV2(num_filters))

            self.convs.add(gluon.nn.Conv2D(num_filters, 3, padding=1))
            self.convs.add(gluon.nn.LeakyReLU(alpha=0.3))

            self.value = gluon.nn.HybridSequential()
            self.value.add(gluon.nn.Conv2D(2, 3, padding=1))
            self.value.add(gluon.nn.LeakyReLU(alpha=0.3))
            self.value.add(gluon.nn.Flatten())
            self.value.add(gluon.nn.Dense(num_filters))
            self.value.add(gluon.nn.LeakyReLU(alpha=0.3))
            self.value.add(gluon.nn.Dense(1))

            self.policy = gluon.nn.HybridSequential()
            self.policy.add(gluon.nn.Conv2D(2, 3, padding=1))
            self.policy.add(gluon.nn.LeakyReLU(alpha=0.3))
            self.policy.add(gluon.nn.Flatten())
            self.policy.add(gluon.nn.Dense((9 * 9 + 1) * 2))
            self.policy.add(gluon.nn.LeakyReLU(alpha=0.3))
            self.policy.add(gluon.nn.Dense(9 * 9 + 1))

    def hybrid_forward(self, F, x):
        x = self.convs(x)
        p = self.policy(x)
        v = self.value(x)

        return F.softmax(p), F.sigmoid(v), p, v

net = CombinedNet(64, 3)
net.collect_params().initialize(mx.init.MSRAPrelu(), ctx=ctx)
net.hybridize()

policy_loss = gluon.loss.SoftmaxCrossEntropyLoss(sparse_label=False)
policy_loss.hybridize()

value_loss = gluon.loss.SigmoidBCELoss()
value_loss.hybridize()

trainer = gluon.Trainer(net.collect_params(), 'NAG', {'learning_rate': .1, 'momentum': .9, 'wd': 1e-4})

def rmean(series, win=1000):
    return np.mean(series[-win:])

closses = []
vlosses = []
plosses = []
vaccs = []
paccs = []

val_closses = []
val_vlosses = []
val_plosses = []

def pred_val_set(x,y):
    # Predict Validation set and append losses
    py = y[:, :(9*9+1)]
    vy = y[:, (9*9+1)]

    _,_,pp,vp = net(x)
    vloss = value_loss(vp, vy)
    ploss = policy_loss(pp, py)
    closs = (0.1 * vloss.mean() + ploss.mean())

    val_closses.append(closs.as_in_context(mx.cpu()).asnumpy()[0])
    val_vlosses.append(vloss.as_in_context(mx.cpu()).asnumpy()[0])
    val_plosses.append(ploss.as_in_context(mx.cpu()).asnumpy()[0])

def wrap_iter(it):
    while True:
        try:
            yield it.next()
        except StopIteration:
            pred_val_set(val_set_x,val_set_y)
            update_data()
            it.reset()

def early_stopping(last = 3):
    if len(val_closses) >= 3:
        return val_closses[-3]<val_closses[-2] and val_closses[-2]<val_closses[-1]
    return False

def random_augmentation(x,y):
    for i in range(x.shape[0]):
        seed = np.random.randint(low=0, high=2, size=3, dtype=int)
        y_probs = y[i,:-2].copy().reshape((9, 9))
        if seed[0]:
            x[i] = nd.transpose(x[i], (0, 2, 1))
            y_probs = nd.transpose(y_probs)
        if seed[1]:
            x[i] = nd.flip(x[i], 1)
            y_probs = nd.flip(y_probs, 0)
        if seed[2]:
            x[i] = nd.flip(x[i], 2)
            y_probs = nd.flip(y_probs, 1)
        y[i,:-2] = y_probs.reshape((9 * 9,))
    return x,y

sigmoid = gluon.nn.Activation('sigmoid')

pred_val_set(val_set_x,val_set_y)

for i, batch in enumerate(wrap_iter(diter)):
    x = batch.data[0].as_in_context(ctx)
    y = batch.label[0].as_in_context(ctx)
    x,y = random_augmentation(x,y)
    py = y[:, :(9*9+1)]
    vy = y[:, (9*9+1)]

    with autograd.record():
        _, _, pp, vp = net(x)
        vloss = value_loss(vp, vy)
        ploss = policy_loss(pp, py)

        combined_loss = (0.1 * vloss.mean() + ploss.mean())
        combined_loss.backward()

    closses.append(combined_loss.as_in_context(mx.cpu()).asnumpy()[0])
    vlosses.append(vloss.as_in_context(mx.cpu()).asnumpy()[0])
    plosses.append(ploss.as_in_context(mx.cpu()).asnumpy()[0])
    paccs.append((pp.argmax(axis=-1) == py.argmax(axis=-1)).mean().as_in_context(mx.cpu()).asnumpy()[0] * 100)
    vaccs.append(((sigmoid(vp) > .5).flatten()[:, 0] == vy).mean().as_in_context(mx.cpu()).asnumpy()[0] * 100)

    trainer.step(batch_size)

    sys.stdout.write('\r{}: C:{:.3f}, V:{:.3f} ({:.1f}%), P:{:.3f} ({:.1f}%), ValC {:.3f}'.format(
        i, rmean(closses), rmean(vlosses), rmean(vaccs), rmean(plosses), rmean(paccs),val_closses[-1]))

    if early_stopping() or i == 1000000:
        print("Training stopped")
        break

np.mean(paccs[-1000:])

np.mean(vaccs[-1000:])

net.export('/Users/valentinwolf/Documents/Studium/Machine_Learning/SoftwareProjekt')