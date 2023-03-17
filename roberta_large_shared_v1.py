# -*- coding: utf-8 -*-
"""Roberta_large_shared.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/11XwO8dyr0JXlg_rcyZQCT66vAPHSsP1I
"""

!pip install transformers
!pip install torchdata
!pip install -U torchtext

import random
import torch
import numpy as np
from torch.utils.data.dataset import ConcatDataset
from torchtext.datasets import RTE,MRPC
torch.manual_seed(2022)
random.seed(2022)

import torch
from torch import nn
import torch.nn.functional as F
from torch.nn import CrossEntropyLoss, MSELoss
from transformers import RobertaPreTrainedModel, RobertaModel,RobertaConfig
class RobertaForSequenceClassification(RobertaPreTrainedModel):
    def __init__(self,config):
        super().__init__(config)
        self.num_labels = 2
        #config = BertConfig()
        self.roberta = RobertaModel(config)
        self.dropout = nn.Dropout(0.2)
        self.deterministic = False
        self.ib_dim = 384
        self.ib = True
        #self.deterministic = True
        self.activation = 'relu'
        self.activations = {'tanh': nn.Tanh(), 'relu': nn.ReLU(), 'sigmoid': nn.Sigmoid()}
        if self.ib or self.deterministic:
            self.kl_annealing = "linear"
            self.hidden_dim = (1024 + self.ib_dim) // 2
            intermediate_dim = (self.hidden_dim+1024)//2
            self.mlp = nn.Sequential(
                nn.Linear(1024, intermediate_dim), #768
                self.activations[self.activation],
                nn.Linear(intermediate_dim, self.hidden_dim),
                self.activations[self.activation])
            self.beta = 1e-03
            self.sample_size = 5 
            self.emb2mu_rte = nn.Linear(self.hidden_dim, self.ib_dim)
            self.emb2std_rte = nn.Linear(self.hidden_dim, self.ib_dim)
            self.mu_p_rte = nn.Parameter(torch.randn(self.ib_dim))
            self.std_p_rte = nn.Parameter(torch.randn(self.ib_dim))
            self.emb2mu_mrpc = nn.Linear(self.hidden_dim, self.ib_dim)
            self.emb2std_mrpc = nn.Linear(self.hidden_dim, self.ib_dim)
            self.mu_p_mrpc = nn.Parameter(torch.randn(self.ib_dim))
            self.std_p_mrpc = nn.Parameter(torch.randn(self.ib_dim))
            self.classifier_rte = nn.Linear(self.ib_dim, self.num_labels)
            self.classifier_mrpc = nn.Linear(self.ib_dim, self.num_labels) 
        else:
            self.classifier = nn.Linear(1024, self.num_labels)

        self.init_weights()

    def estimate(self, emb, emb2mu, emb2std):
        """Estimates mu and std from the given input embeddings."""
        mean = emb2mu(emb)
        std = torch.nn.functional.softplus(emb2std(emb))
        return mean, std

    def kl_div(self, mu_q, std_q, mu_p, std_p):
        """Computes the KL divergence between the two given variational distribution.\
           This computes KL(q||p), which is not symmetric. It quantifies how far is\
           The estimated distribution q from the true distribution of p."""
        k = mu_q.size(1)
        mu_diff = mu_p - mu_q
        mu_diff_sq = torch.mul(mu_diff, mu_diff)
        logdet_std_q = torch.sum(2 * torch.log(torch.clamp(std_q, min=1e-8)), dim=1)
        logdet_std_p = torch.sum(2 * torch.log(torch.clamp(std_p, min=1e-8)), dim=1)
        fs = torch.sum(torch.div(std_q ** 2, std_p ** 2), dim=1) + torch.sum(torch.div(mu_diff_sq, std_p ** 2), dim=1)
        kl_divergence = (fs - k + logdet_std_p - logdet_std_q)*0.5
        return kl_divergence.mean()

    def reparameterize(self, mu, std):
        batch_size = mu.shape[0]
        z = torch.randn(self.sample_size, batch_size, mu.shape[1]).cuda()
        return mu + std * z

    def get_logits(self, z, mu, sampling_type,dataset_name):
        if sampling_type == "iid":
            if dataset_name == "rte":
              logits = self.classifier_rte(z)
            else:
              logits = self.classifier_mrpc(z)
            mean_logits = logits.mean(dim=0)
            logits = logits.permute(1, 2, 0)
        else:
            if dataset_name == 0:
              mean_logits = self.classifier_rte(mu)
            else:
              mean_logits = self.classifier_mrpc(mu)
            #mean_logits = self.classifier(mu)
            logits = mean_logits
        return logits, mean_logits


    def sampled_loss(self, logits, mean_logits, labels, sampling_type):
        if sampling_type == "iid":
            # During the training, computes the loss with the sampled embeddings.
            if self.num_labels == 1:
                #  We are doing regression
                loss_fct = MSELoss()
                loss = loss_fct(logits.view(-1, self.sample_size), labels[:, None].float().expand(-1, self.sample_size))
                loss = torch.mean(loss, dim=-1)
                loss = torch.mean(loss, dim=0)
            else:
                loss_fct = CrossEntropyLoss(reduce=False)
                loss = loss_fct(logits, labels[:, None].expand(-1, self.sample_size))
                loss = torch.mean(loss, dim=-1)
                loss = torch.mean(loss, dim=0)
        else:
            # During test time, uses the average value for prediction.
            if self.num_labels == 1:
                loss_fct = MSELoss()
                loss = loss_fct(mean_logits.view(-1), labels.float().view(-1))
            else:
                loss_fct = CrossEntropyLoss()
                loss = loss_fct(mean_logits, labels)
        return loss

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        labels=None,
        sampling_type="iid",
        epoch=1,
        **kwargs
        #dataset_name="rte",
    ):
        r"""
        labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size,)`, `optional`, defaults to :obj:`None`):
            Labels for computing the sequence classification/regression loss.
            Indices should be in :obj:`[0, ..., config.num_labels - 1]`.
            If :obj:`config.num_labels == 1` a regression loss is computed (Mean-Square loss),
            If :obj:`config.num_labels > 1` a classification loss is computed (Cross-Entropy).
    Returns:
        :obj:`tuple(torch.FloatTensor)` comprising various elements depending on the configuration (:class:`~transformers.BertConfig`) and inputs:
        loss (:obj:`torch.FloatTensor` of shape :obj:`(1,)`, `optional`, returned when :obj:`label` is provided):
            Classification (or regression if config.num_labels==1) loss.
        logits (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, config.num_labels)`):
            Classification (or regression if config.num_labels==1) scores (before SoftMax).
        hidden_states (:obj:`tuple(torch.FloatTensor)`, `optional`, returned when ``config.output_hidden_states=True``):
            Tuple of :obj:`torch.FloatTensor` (one for the output of the embeddings + one for the output of each layer)
            of shape :obj:`(batch_size, sequence_length, hidden_size)`.
            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        attentions (:obj:`tuple(torch.FloatTensor)`, `optional`, returned when ``config.output_attentions=True``):
            Tuple of :obj:`torch.FloatTensor` (one for each layer) of shape
            :obj:`(batch_size, num_heads, sequence_length, sequence_length)`.
            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
            heads.
    Examples::
        from transformers import BertTokenizer, BertForSequenceClassification
        import torch
        tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
        model = BertForSequenceClassification.from_pretrained('bert-base-uncased')
        input_ids = torch.tensor(tokenizer.encode("Hello, my dog is cute", add_special_tokens=True)).unsqueeze(0)  # Batch size 1
        labels = torch.tensor([1]).unsqueeze(0)  # Batch size 1
        outputs = model(input_ids, labels=labels)
        loss, logits = outputs[:2]
        """
        #dataset_name="rte"
        #print(position_ids.item())
        position_id = None
        final_outputs = {}
        outputs = self.roberta(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_id,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
        )
        pooled_output = outputs[1]
        pooled_output = self.dropout(pooled_output)
        loss = {}

        if self.deterministic:
            pooled_output = self.mlp(pooled_output)
            mu, std = self.estimate(pooled_output, self.emb2mu, self.emb2std)
            final_outputs["z"] = mu
            sampled_logits, logits = self.get_logits(mu, mu, sampling_type='argmax',dataset_name=kwargs["dataset_name"]) # always deterministic
            if labels is not None:
                loss["loss"] = self.sampled_loss(sampled_logits, logits, labels.view(-1), sampling_type='argmax')

        elif self.ib:
            if kwargs["dataset_name"]=="rte":
              pooled_output = self.mlp(pooled_output)
              batch_size = pooled_output.shape[0]
              mu, std = self.estimate(pooled_output, self.emb2mu_rte, self.emb2std_rte)
              mu_p = self.mu_p_rte.view(1, -1).expand(batch_size, -1)
              std_p = torch.nn.functional.softplus(self.std_p_rte.view(1, -1).expand(batch_size, -1))
            else:
              pooled_output = self.mlp(pooled_output)
              batch_size = pooled_output.shape[0]
              mu, std = self.estimate(pooled_output, self.emb2mu_mrpc, self.emb2std_mrpc)
              mu_p = self.mu_p_mrpc.view(1, -1).expand(batch_size, -1)
              std_p = torch.nn.functional.softplus(self.std_p_mrpc.view(1, -1).expand(batch_size, -1))
            kl_loss = self.kl_div(mu, std, mu_p, std_p)
            z = self.reparameterize(mu, std)
            final_outputs["z"] = mu

            if self.kl_annealing == "linear":
                beta = min(1.0, epoch*self.beta)
                 
            sampled_logits, logits = self.get_logits(z, mu, sampling_type,dataset_name=kwargs["dataset_name"])
            #print(labels)
            if labels is not None:
                if kwargs["label"] is not None:
                  ce_loss_rte = self.sampled_loss(kwargs["sampled_logits"], kwargs["logits"], kwargs["label"].view(-1), sampling_type)
                  total_loss_rte = ce_loss_rte + (beta if self.kl_annealing == "linear" else self.beta) * kwargs["kl_loss"]
                  ce_loss_mrpc = self.sampled_loss(sampled_logits, logits, labels.view(-1), sampling_type)
                  total_loss_mrpc = ce_loss_mrpc + (beta if self.kl_annealing == "linear" else self.beta) * kl_loss
                  total_loss = total_loss_rte + total_loss_mrpc
                else:
                  ce_loss = self.sampled_loss(sampled_logits, logits, labels.view(-1), sampling_type)
                  total_loss = ce_loss + (beta if self.kl_annealing == "linear" else self.beta) * kl_loss
                loss["loss"] = total_loss
        else:
            final_outputs["z"] = pooled_output
            logits = self.classifier(pooled_output)
            if labels is not None:
                if self.num_labels == 1:
                    #  We are doing regression
                    loss_fct = MSELoss()
                    loss["loss"] = loss_fct(logits.view(-1), labels.float().view(-1))
                else:
                    loss_fct = CrossEntropyLoss()
                    loss["loss"] = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
                    
        final_outputs.update({"logits": logits, "loss": loss, "hidden_attention": outputs[2:],"sampled_logits":sampled_logits,"kl_loss":kl_loss})
        return final_outputs

class RTE_data(torch.utils.data.Dataset):
  def __init__(self,data_type):
    self.labels = []
    self.input_1 = []
    self.input_2 = []
    self.rte_train_iter = RTE(split=data_type)
    for label,inp1,inp2 in self.rte_train_iter:
      self.labels.append(label)
      self.input_1.append(inp1)
      self.input_2.append(inp2)
    
  def __getitem__(self,idx):
    return self.labels[idx],self.input_1[idx],self.input_2[idx],"rte"
  def __len__(self):
    return len(self.labels)

class MRPC_data(torch.utils.data.Dataset):
  def __init__(self,data_type):
    self.labels = []
    self.input_1 = []
    self.input_2 = []
    
    if data_type == "train" or data_type == "dev":
      self.mrpc_train_iter = MRPC(split="train")
    else:
      self.mrpc_train_iter = MRPC(split=data_type)
    for label,inp1,inp2 in self.mrpc_train_iter:
      self.labels.append(label)
      self.input_1.append(inp1)
      self.input_2.append(inp2)

    print(int(len(self.labels)*0.8))
    if data_type == "train":
      self.labels = self.labels[:int(len(self.labels)*0.8)]
      self.input_1 = self.input_1[:int(len(self.input_1)*0.8)]
      self.input_2 = self.input_2[:int(len(self.input_2)*0.8)]
    if data_type == "dev":
      self.labels = self.labels[int(len(self.labels)*0.8):]
      self.input_1 = self.input_1[int(len(self.input_1)*0.8):]
      self.input_2 = self.input_2[int(len(self.input_2)*0.8):]
  def __getitem__(self,idx):
    return self.labels[idx],self.input_1[idx],self.input_2[idx],"mrpc"
  def __len__(self):
    return len(self.labels)

import math
import torch
from torch.utils.data.sampler import RandomSampler


class BatchSchedulerSampler(torch.utils.data.sampler.Sampler):
    """
    iterate over tasks and provide a random batch per task in each mini-batch
    """
    def __init__(self, dataset, batch_size):
        self.dataset = dataset
        self.batch_size = batch_size
        self.number_of_datasets = len(dataset.datasets)
        self.largest_dataset_size = max([len(cur_dataset.labels) for cur_dataset in dataset.datasets])

    def __len__(self):
        return self.batch_size * math.ceil(self.largest_dataset_size / self.batch_size) * len(self.dataset.datasets)

    def __iter__(self):
        samplers_list = []
        sampler_iterators = []
        for dataset_idx in range(self.number_of_datasets):
            cur_dataset = self.dataset.datasets[dataset_idx]
            sampler = RandomSampler(cur_dataset)
            samplers_list.append(sampler)
            cur_sampler_iterator = sampler.__iter__()
            sampler_iterators.append(cur_sampler_iterator)

        push_index_val = [0] + self.dataset.cumulative_sizes[:-1]
        step = self.batch_size * self.number_of_datasets
        samples_to_grab = self.batch_size
        # for this case we want to get all samples in dataset, this force us to resample from the smaller datasets
        epoch_samples = self.largest_dataset_size * self.number_of_datasets

        final_samples_list = []  # this is a list of indexes from the combined dataset
        for _ in range(0, epoch_samples, step):
            for i in range(self.number_of_datasets):
                cur_batch_sampler = sampler_iterators[i]
                cur_samples = []
                for _ in range(samples_to_grab):
                    try:
                        cur_sample_org = cur_batch_sampler.__next__()
                        cur_sample = cur_sample_org + push_index_val[i]
                        cur_samples.append(cur_sample)
                    except StopIteration:
                        # got to the end of iterator - restart the iterator and continue to get samples
                        # until reaching "epoch_samples"
                        sampler_iterators[i] = samplers_list[i].__iter__()
                        cur_batch_sampler = sampler_iterators[i]
                        cur_sample_org = cur_batch_sampler.__next__()
                        cur_sample = cur_sample_org + push_index_val[i]
                        cur_samples.append(cur_sample)
                final_samples_list.extend(cur_samples)

        return iter(final_samples_list)

rte_data = RTE_data("train")
mrpc_data = MRPC_data("train")
concat = ConcatDataset([rte_data,mrpc_data])

train_dataloader = torch.utils.data.DataLoader(dataset=concat,
                                         sampler=BatchSchedulerSampler(dataset=concat,
                                                                       batch_size=32),
                                         batch_size=32,
                                         shuffle=False)

dev_rte_data = RTE_data("dev")
dev_mrpc_data = MRPC_data("dev")
dev_concat = ConcatDataset([dev_rte_data,dev_mrpc_data])

dev_dataloader = torch.utils.data.DataLoader(dataset=dev_concat,
                                         sampler=BatchSchedulerSampler(dataset=dev_concat,
                                                                       batch_size=32),
                                         batch_size=32,
                                         shuffle=False)

test_mrpc_data = MRPC_data("test")
mrpc_test_dataloader = torch.utils.data.DataLoader(dataset=test_mrpc_data,
                                         batch_size=32,
                                         shuffle=True)

from transformers import RobertaTokenizer

tokenizer = RobertaTokenizer.from_pretrained('roberta-large', do_lower_case=True)
config = RobertaConfig.from_pretrained(
        "roberta-large",
        num_labels=2)
viroberta_large = RobertaForSequenceClassification.from_pretrained(
        "roberta-large",
        config=config
)

for name, param in viroberta_large.named_parameters():
    if param.requires_grad:
        print(name)

from transformers import AdamW
from transformers import get_linear_schedule_with_warmup
no_decay = ["bias", "LayerNorm.weight"]
optimizer_grouped_parameters = [
        {
            "params": [p for n, p in viroberta_large.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": 0.0,
        },
        {"params": [p for n, p in viroberta_large.named_parameters() if any(nd in n for nd in no_decay)], "weight_decay": 0.0},
]
optimizer = AdamW(optimizer_grouped_parameters, lr=5e-5, eps=1e-8) #lr was 5e-5
scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=0,
                                                num_training_steps=len(train_dataloader)*50)

import warnings
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

import transformers
transformers.logging.set_verbosity_error()

def set_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    #if args.n_gpu > 0:
    #    torch.cuda.manual_seed_all(args.seed)
    torch.cuda.manual_seed(seed)

import transformers
from tqdm import tqdm
from sklearn.metrics import accuracy_score,f1_score

n_epochs = 10 #10
device="cuda"
#viroberta_large.train()
viroberta_large.to(device)
viroberta_large.zero_grad()
set_seed(2022)
train_loss_rte=[]
train_loss_mrpc=[]
dev_loss_rte=[]
dev_loss_mrpc=[]
train_acc_rte=[]
train_acc_mrpc=[]
dev_acc_rte=[]
dev_acc_mrpc=[]
total_loss=[]

for epoch in range(n_epochs):
  total_train_loss_rte = 0
  total_train_loss_mrpc = 0
  total_dev_loss_rte = 0
  total_dev_loss_mrpc = 0
  total_train_acc_rte = 0
  total_train_acc_mrpc = 0
  total_dev_acc_rte = 0
  total_dev_acc_mrpc = 0
  total_train_loss=0
  viroberta_large.train()
  c1_train=0
  c2_train=0
  c=0
  logs=None
  labs = None
  sampled_labs = None
  kl_loss= None
  for labels,inp1,inp2,st in tqdm(train_dataloader):
    
    optimizer.zero_grad()
    batch = tokenizer(text=inp1,text_pair=inp2,max_length=128,truncation=True,padding=True,add_special_tokens=True,is_split_into_words=False,return_tensors='pt')
    batch.to(device)

    #with torch.set_grad_enabled(True):
    out = viroberta_large(batch["input_ids"],token_type_ids=None, 
                            attention_mask=batch["attention_mask"],labels=labels.to(device),dataset_name=st[0],logits=logs,label=labs,sampled_logits=sampled_labs,kl_loss=kl_loss)
    
    #loss = (loss*0.5)/2 
    logs= out["logits"].to(device)
    labs=labels.to(device)
    sampled_labs = out["sampled_logits"].to(device)
    kl_loss = out["kl_loss"]
    f=0
    if st[0]=="rte":
      c1_train+=1
    if st[0]=="mrpc":
      c2_train+=1
    
    if c1_train == c2_train:
      loss = out["loss"]["loss"]  
      total_train_loss+=loss.item()
      c+=1
      loss.backward()
      optimizer.step()
      scheduler.step()  # Update learning rate schedule
      viroberta_large.zero_grad()
      logs=None
      labs = None
      sampled_labs=None
      kl_loss = None
      
      f=1
      torch.nn.utils.clip_grad_norm_(viroberta_large.parameters(), 1.0)
    if st[0]=="rte":
        out = viroberta_large(batch["input_ids"],token_type_ids=None, 
                            attention_mask=batch["attention_mask"],labels=labels.to(device),dataset_name=st[0],logits=None,label=None,sampled_logits=None,kl_loss=None)
        loss = out["loss"]["loss"]
        total_train_loss_rte += loss.item()
        total_train_acc_rte += accuracy_score(labels.cpu(),torch.argmax(out["logits"],1).cpu())  
    if st[0]=="mrpc":
      #total_train_loss_mrpc += loss.item()
      out = viroberta_large(batch["input_ids"],token_type_ids=None, 
                            attention_mask=batch["attention_mask"],labels=labels.to(device),dataset_name=st[0],logits=None,label=None,sampled_logits=None,kl_loss=None)
      loss = out["loss"]["loss"]
      total_train_loss_mrpc += loss.item()
      total_train_acc_mrpc += accuracy_score(labels.cpu(),torch.argmax(out["logits"],1).cpu())
    #if f==0 and c1_train+c2_train>=len(train_dataloader):
    #  optimizer.step()
    #  scheduler.step()  # Update learning rate schedule
    #  viroberta_large.zero_grad()
  print("Epoch "+str(epoch)+" RTE Train Loss: "+str(total_train_loss/len(train_dataloader)))
  print("Epoch "+str(epoch)+" RTE Train Loss: "+str(total_train_loss_rte/c1_train)+" MRPC Train Loss: "+str(total_train_loss_mrpc/c2_train))
  print(" RTE Train Acc: "+str(total_train_acc_rte/c1_train)+" MRPC Train Acc: "+str(total_train_acc_mrpc/c2_train))
  total_loss.append(total_train_loss/c)
  train_loss_rte.append(total_train_loss_rte/c1_train)
  train_loss_mrpc.append(total_train_loss_mrpc/c2_train)
  train_acc_rte.append(total_train_acc_rte/c1_train)
  train_acc_mrpc.append(total_train_acc_mrpc/c2_train)
  

  
  viroberta_large.eval()
  c1_dev=0
  c2_dev=0
  for labels,inp1,inp2,st in tqdm(dev_dataloader):
    
    batch = tokenizer(text=inp1,text_pair=inp2,max_length=128,truncation=True,padding=True,is_split_into_words=False,return_tensors='pt')
    batch.to(device)
    
    out = viroberta_large(batch["input_ids"],token_type_ids=None, 
                             attention_mask=batch["attention_mask"],labels=labels.to(device),dataset_name=st[0],logits=None,label=None,sampled_logits=None,kl_loss=None)
    loss = out["loss"]["loss"]
    if st[0]=="rte":
      total_dev_loss_rte += loss.item()
      total_dev_acc_rte += accuracy_score(labels.cpu(),torch.argmax(out["logits"],1).cpu())
      c1_dev+=1
    if st[0]=="mrpc":
      total_dev_loss_mrpc += loss.item()
      total_dev_acc_mrpc += accuracy_score(labels.cpu(),torch.argmax(out["logits"],1).cpu())
      c2_dev+=1
     
  print("Epoch "+str(epoch)+" RTE Val Loss: "+str(total_dev_loss_rte/c1_dev)+" MRPC Val Loss: "+str(total_dev_loss_mrpc/c2_dev))
  print(" RTE Val Acc: "+str(total_dev_acc_rte/c1_dev)+" MRPC Val Acc: "+str(total_dev_acc_mrpc/c2_dev))
  
  dev_loss_rte.append(total_dev_loss_rte/c1_dev)
  dev_loss_mrpc.append(total_dev_loss_mrpc/c2_dev)
  dev_acc_rte.append(total_dev_acc_rte/c1_dev)
  dev_acc_mrpc.append(total_dev_acc_mrpc/c2_dev)
  torch.save(viroberta_large.state_dict(), "viroberta_large_70")


print("Total_train_loss : ",total_loss)
print("RTE Train Loss: ",train_loss_rte)
print("MRPC Train Loss: ",train_loss_mrpc)
print("RTE Dev Loss: ",dev_loss_rte)
print("MRPC Dev Loss: ",dev_loss_mrpc)
print("RTE Train Acc: ",train_acc_rte)
print("MRPC Train Acc: ",train_acc_mrpc)
print("RTE Dev Acc: ",dev_acc_rte)
print("MRPC Dev Acc: ",dev_acc_mrpc)

from tqdm import tqdm
device="cuda"
#vibert.train()
viroberta_large.to(device)
set_seed(2022)
viroberta_large.eval()
total_test_loss_rte = 0
total_test_loss_mrpc = 0
total_test_acc_rte = 0
total_test_acc_mrpc = 0
total_test_f1_rte = 0
total_test_f1_mrpc = 0
c1=1
c2=0
epoch=1
for labels,inp1,inp2,st in tqdm(mrpc_test_dataloader):
  
  batch = tokenizer(text=inp1,text_pair=inp2,max_length=128,truncation=True,padding=True,is_split_into_words=False,return_tensors='pt')
  batch.to(device)
  out = viroberta_large(batch["input_ids"],token_type_ids=None, 
                             attention_mask=batch["attention_mask"],labels=labels.to(device),dataset_name=st[0],logits=None,label=None,sampled_logits=None,kl_loss=None)
  loss = out["loss"]["loss"]
  if st[0]=="rte":
    total_test_loss_rte += loss.item()
    total_test_acc_rte += accuracy_score(labels.cpu(),torch.argmax(out["logits"],1).cpu())
    total_test_f1_rte += f1_score(labels.cpu(),torch.argmax(out["logits"],1).cpu())
    c1+=1
  if st[0]=="mrpc":
    total_test_loss_mrpc += loss.item()
    total_test_acc_mrpc += accuracy_score(labels.cpu(),torch.argmax(out["logits"],1).cpu())
    total_test_f1_mrpc += f1_score(labels.cpu(),torch.argmax(out["logits"],1).cpu())
    c2+=1
    
print("Epoch "+str(epoch)+" RTE Test Loss: "+str(total_test_loss_rte/c1)+" MRPC Test Loss: "+str(total_test_loss_mrpc/c2))
print(" RTE Test Acc: "+str(total_test_acc_rte/c1)+" MRPC Test Acc: "+str(total_test_acc_mrpc/c2))
print(" RTE Test F1: "+str(total_test_f1_rte/c1)+" MRPC Test F1: "+str(total_test_f1_mrpc/c2))