from typing import Any, Optional
from pytorch_lightning import LightningModule, LightningDataModule
import pandas as pd
from pytorch_lightning.utilities.types import EVAL_DATALOADERS, STEP_OUTPUT
from torchmetrics import Metric
from datasets import Dataset
from pytorch_lightning.utilities.data import DataLoader
import os
import torch
from utils import format_example, gen_prompt
import numpy as np


class LogitAccuracy(Metric):
    def __init__(self, tokenizer, model_name):
        super().__init__()
        self.tokenizer = tokenizer
        self.model_name = model_name

        self.all_preds = []
        self.all_golds = []

        self.add_state("total", default=torch.tensor(0), dist_reduce_fx="sum")
        self.add_state("correct", default=torch.tensor(0), dist_reduce_fx="sum")
    
    def update(self, logits, labels):
        self.total += logits.shape[0]
        labels = self.tokenizer.batch_decode(labels, skip_special_tokens=True)

        if "alpaca" in self.model_name:
            logits = logits[:, -1, :]
        else: 
            logits = logits[:, 0, :]

        preds = []
        
        for i in range(logits.shape[0]):
            label = labels[i]
            logit = logits[i].flatten()
            probs = (
                torch.nn.functional.softmax(
                    torch.tensor(
                        [
                            logit[self.tokenizer("A").input_ids[0]],
                            logit[self.tokenizer("B").input_ids[0]],
                            logit[self.tokenizer("C").input_ids[0]],
                            logit[self.tokenizer("D").input_ids[0]],
                        ]
                    ),
                    dim=0,
                )
                .detach()
                .cpu()
                .numpy()
            )
            if "alpaca" in self.model_name:
                probs = (
                    torch.nn.functional.softmax(
                        torch.tensor(
                            [
                                logit[self.tokenizer("A").input_ids[-1]],
                                logit[self.tokenizer("B").input_ids[-1]],
                                logit[self.tokenizer("C").input_ids[-1]],
                                logit[self.tokenizer("D").input_ids[-1]],
                            ]
                        ),
                        dim=0,
                    )
                    .detach()
                    .cpu()
                    .numpy()
                )
            pred = {0: "A", 1: "B", 2: "C", 3: "D"}[np.argmax(probs)]
            preds.append(pred)
            self.correct += pred.strip() == label.strip()

        self.all_preds.extend(preds)
        self.all_golds.extend(labels)

    def compute(self):
        return self.correct.float() / self.total.float()


class MMLUDataModule(LightningDataModule):
    def __init__(self, data_dir, prompt_dir, tokenizer, batch_size, subj, ntrain=0):
        super(MMLUDataModule).__init__()
        self.subject = subj
        self.batch_size = batch_size
        self.ntrain = ntrain
        self.tokenizer = tokenizer

        self.dev_df = pd.read_csv(
            os.path.join(data_dir, "dev", subj + "_dev.csv"), header=None
        )

        self.test_df = pd.read_csv(
            os.path.join(data_dir, "test", subj + "_test.csv"), header=None
        )
        self.prompt_dir = prompt_dir

    def prepare_data(self):
        self.testset = []
        self.valset = []

        if self.prompt_dir is None:
            for i in self.test_df.index:

                k = self.ntrain
                prompt_end = format_example(self.test_df, i, include_answer=False)
                train_prompt = gen_prompt(self.dev_df, self.subject, k)
                prompt = train_prompt + prompt_end

                while self.tokenizer(prompt, return_tensors="pt").input_ids.shape[-1] > 2048:
                    k -= 1
                    train_prompt = gen_prompt(self.dev_df, self.subject, k)
                    prompt = train_prompt + prompt_end

                label = self.test_df.iloc[i, self.test_df.shape[1] - 1]
            
                self.testset.append({
                    "input": prompt,
                    "output": label
                })

            for i in self.dev_df.index:

                prompt_end = format_example(self.dev_df, i, include_answer=False)
                train_prompt = gen_prompt(self.dev_df, self.subject, 0)
                prompt = train_prompt + prompt_end

                while self.tokenizer(prompt, return_tensors="pt").input_ids.shape[-1] > 2048:
                    k -= 1
                    train_prompt = gen_prompt(self.dev_df, self.subject, 0)
                    prompt = train_prompt + prompt_end

                label = self.dev_df.iloc[i, self.dev_df.shape[1] - 1]
            
                self.valset.append({
                    "input": prompt,
                    "output": label
                })
        else:
            raise NotImplementedError
        
        self.valset = Dataset.from_list(self.valset)
        self.testset = Dataset.from_list(self.testset)


    def collate_fn(self, batch):
        batch = [b.values() for b in batch]
        input_text, output_text = list(zip(*batch))
        assert len(input_text) == len(output_text)
        batch = self.tokenizer(text=input_text, text_target=output_text, padding='longest', truncation=True, return_tensors="pt", max_length=512)
        return batch
    
    def val_dataloader(self):
        return DataLoader(self.valset, batch_size=self.batch_size, collate_fn=self.collate_fn)

    def test_dataloader(self):
        return DataLoader(self.testset, batch_size=self.batch_size, collate_fn=self.collate_fn)


class MMLUModel(LightningModule):
    
    def __init__(self, model, tokenizer, args):
        super().__init__()
        self.model = model
        self.model_name = args.model
        self.tokenizer = tokenizer
        self.val_loss = []
        self.args = args
        self.metric = LogitAccuracy(tokenizer, self.model_name)
    
    def on_test_start(self):
        self.model.eval()
        self.metric.reset()
        self.metric.all_golds = []
        self.metric.all_preds = []
        return super().on_test_start()
    
    def test_step(self, batch, batch_idx):
        
        if "alpaca" in self.model_name:
            labels = batch.pop("labels")
        else:
            labels = batch["labels"]
        outputs = self.model(**batch)
        logits = outputs.logits

        self.metric.update(logits, labels)

    def on_validation_start(self) -> None:
        self.model.eval()
        self.val_loss = []
        return super().on_validation_start()
    
    def validation_step(self, batch, batch_idx):
        if "alpaca" in self.model_name:
            raise NotImplementedError
        outputs = self.model(**batch)
        loss = outputs.loss
        self.val_loss.append(loss.item())

    def get_val_loss(self):
        return np.mean(self.val_loss)
    
    def save_preds(self, path):
        with open(path, "w") as f:
            for pred, gold in zip(self.metric.all_preds, self.metric.all_golds):
                f.write(f"{pred}\t{gold}\n")
