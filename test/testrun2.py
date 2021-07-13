from torch.utils.data.dataloader import DataLoader
from data.Datasets import cifar_dataset, mnist_dataset
from model.SelfAttention import ViT
import torch

dev = torch.device("cuda:0")
#dev = torch.device("cpu")
dataset = mnist_dataset("datasets", True)
dataloader = DataLoader(dataset, batch_size=8)
net = ViT((28, 28), (1, 1), 16, 10, img_channel=1, dev=dev, sa_num=16, msa_num=6).to(dev)
l = torch.nn.CrossEntropyLoss()
opt = torch.optim.SGD(net.parameters(), lr=3e-4, momentum=0.9)

def train():
	opt_epoch = 32
	running_loss = 0.0
	for epoch, d in enumerate(dataloader):
		(x, y) = d
		x = x.to(dev)
		y = y.to(dev)

		y_pred = net(x)
		loss = l(y_pred, y)
		loss.backward()
		opt.step()
		opt.zero_grad()

		if (epoch % opt_epoch == 0):
			print(f"epoch = {epoch}, loss = {running_loss/opt_epoch}")
			running_loss = 0.0

		running_loss += loss.item()
