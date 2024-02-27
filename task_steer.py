import sys
sys.path.append("../pyvene/")

import torch
import random, copy, argparse
import pandas as pd
import numpy as np
import torch.nn.functional as F
import seaborn as sns
from tqdm import tqdm, trange
from datasets import Dataset, load_dataset
from torch.utils.data import DataLoader
from transformers import get_linear_schedule_with_warmup
from transformers import DataCollatorForSeq2Seq
from transformers import AutoTokenizer
from transformers.activations import ACT2FN
from transformers import get_linear_schedule_with_warmup
from torch.nn import CrossEntropyLoss
import wandb

from pyvene import (
    IntervenableModel,
    LowRankRotatedSpaceIntervention,
    RepresentationConfig,
    IntervenableConfig,
    ConstantSourceIntervention,
    TrainableIntervention,
    DistributedRepresentationIntervention,
)
from pyvene import create_llama
from pyvene import set_seed, count_parameters
from pyvene.models.layers import LowRankRotateLayer

import io
import json
import os
import re

def _make_r_io_base(f, mode: str):
    if not isinstance(f, io.IOBase):
        f = open(f, mode=mode)
    return f

def _make_w_io_base(f, mode: str):
    if not isinstance(f, io.IOBase):
        f_dirname = os.path.dirname(f)
        if f_dirname != "":
            os.makedirs(f_dirname, exist_ok=True)
        f = open(f, mode=mode)
    return f

def jload(f, mode="r"):
    """Load a .json file into a dictionary."""
    f = _make_r_io_base(f, mode)
    jdict = json.load(f)
    f.close()
    return jdict

def jdump(obj, f, mode="w", indent=4, default=str):
    """Dump a str or dictionary to a file in json format.

    Args:
        obj: An object to be written.
        f: A string path to the location on disk.
        mode: Mode for opening the file.
        indent: Indent for storing json dictionaries.
        default: A function to handle non-serializable entries; defaults to `str`.
    """
    f = _make_w_io_base(f, mode)
    if isinstance(obj, (dict, list)):
        json.dump(obj, f, indent=indent, default=default)
    elif isinstance(obj, str):
        f.write(obj)
    else:
        raise ValueError(f"Unexpected type: {type(obj)}")
    f.close()

def create_directory(path):
    """Create directory if not exist"""
    if not os.path.exists(path):
        os.makedirs(path)
        print(f"Directory '{path}' created successfully.")
    else:
        print(f"Directory '{path}' already exists.")

def extract_answer_number(sentence: str) -> float:
    sentence = sentence.replace(',', '')
    pred = [s for s in re.findall(r'-?\d+\.?\d*', sentence)]
    if not pred:
        return float('inf')
    pred_answer = float(pred[-1])
    if isinstance(pred_answer, str):
        try:
            pred_answer = float(pred_answer)
        except ValueError as e:
            pred_answer = float('inf')
    return pred_answer

def extract_answer_letter(sentence: str) -> str:
    sentence_ = sentence.strip()
    pred_answers = re.findall(r'A|B|C|D|E', sentence_)
    if pred_answers:
        if not pred_answers:
            return ''
        return pred_answers[-1]
    else:
        return ''
        
def chunk(iterable, chunksize):
    # if iterable is a list, we chunk with simple list indexing
    if isinstance(iterable, list):
        return [iterable[i:i+chunksize] for i in range(0, len(iterable), chunksize)]
    # otherwise if iterable is a Hf Dataset, we leverage the select() function to create mini datasets
    elif isinstance(iterable, Dataset):
        chunks = []
        for i in range(0, len(iterable), chunksize):
            if i+chunksize < len(iterable):
                chunks.append(iterable.select(list(range(i, i+chunksize))))
            else:
                chunks.append(iterable.select(list(range(i, len(iterable)))))
        return chunks
    else:
        raise Exception(f"Unrecognizable type of iterable for batchification: {type(iterable)}")
        
def extract_output(pred, trigger=''):
    if not trigger:
        return pred
    # for causallm only, use special trigger to detect new tokens.
    # if cannot find trigger --> generation is too long; default to empty generation
    start = pred.find(trigger)
    if start < 0:
        return ''
    output = pred[start+len(trigger):].lstrip() # left strip any whitespaces
    return output
        
device = "cuda"
IGNORE_INDEX = -100
DEFAULT_PAD_TOKEN = "[PAD]"
DEFAULT_EOS_TOKEN = "</s>"
DEFAULT_BOS_TOKEN = "<s>"
DEFAULT_UNK_TOKEN = "<unk>"

no_header_prompt_template = """\
### Instruction:
%s

### Response:
"""

alpaca_prompt_template = """Below is an instruction that \
describes a task, paired with an input that provides \
further context. Write a response that appropriately \
completes the request.

### Instruction:
%s

### Input:
%s

### Response:
"""

alpaca_prompt_no_input_template = """Below is an instruction that \
describes a task. Write a response that appropriately \
completes the request.

### Instruction:
%s

### Response:
"""

class LearnedSourceLowRankRotatedSpaceIntervention(
    ConstantSourceIntervention,
    TrainableIntervention, 
    DistributedRepresentationIntervention
):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        rotate_layer = LowRankRotateLayer(self.embed_dim, kwargs["low_rank_dimension"])
        self.rotate_layer = torch.nn.utils.parametrizations.orthogonal(rotate_layer)
        self.learned_source = torch.nn.Parameter(
            torch.rand(kwargs["low_rank_dimension"]), requires_grad=True)
        self.dropout = torch.nn.Dropout(0.05)
        
    def forward(
        self, base, source=None, subspaces=None
    ):
        rotated_base = self.rotate_layer(base)
        output = base + torch.matmul(
            (self.learned_source - rotated_base), self.rotate_layer.weight.T
        )
        return self.dropout(output.to(base.dtype))

class ConditionedSourceLowRankRotatedSpaceIntervention(
    ConstantSourceIntervention,
    TrainableIntervention, 
    DistributedRepresentationIntervention
):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        rotate_layer = LowRankRotateLayer(self.embed_dim, kwargs["low_rank_dimension"])
        self.rotate_layer = torch.nn.utils.parametrizations.orthogonal(rotate_layer)
        self.learned_source = torch.nn.Linear(
            self.embed_dim, kwargs["low_rank_dimension"]).to(torch.bfloat16)
        self.act_fn = ACT2FN["silu"]
        self.dropout = torch.nn.Dropout(0.05)
        
    def forward(
        self, base, source=None, subspaces=None
    ):
        rotated_base = self.rotate_layer(base)
        output = base + torch.matmul(
            (self.act_fn(self.learned_source(base)) - rotated_base), self.rotate_layer.weight.T
        )
        return self.dropout(output.to(base.dtype))
    
class ConditionedSourceLowRankIntervention(
    ConstantSourceIntervention,
    TrainableIntervention, 
    DistributedRepresentationIntervention
):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.proj_layer = torch.nn.Linear(
            self.embed_dim, kwargs["low_rank_dimension"], bias=False).to(torch.bfloat16)
        self.learned_source = torch.nn.Linear(
            self.embed_dim, kwargs["low_rank_dimension"]).to(torch.bfloat16)
        self.act_fn = ACT2FN["silu"]
        self.dropout = torch.nn.Dropout(0.05)
        
    def forward(
        self, base, source=None, subspaces=None
    ):
        proj_base = self.proj_layer(base)
        output = base + torch.matmul(
            (self.act_fn(self.learned_source(base)) - proj_base), self.proj_layer.weight
        )
        return self.dropout(output.to(base.dtype))
    
def main():
    """
    Generic Representation Finetuning.
    """

    parser = argparse.ArgumentParser(description="A simple script that takes different arguments.")
    
    parser.add_argument('-task', '--task', type=str, default=None)
    parser.add_argument('-train_dataset', '--train_dataset', type=str, default=None)
    parser.add_argument('-eval_dataset', '--eval_dataset', type=str, default=None)
    parser.add_argument('-model', '--model', type=str, help='yahma/llama-7b-hf', default='yahma/llama-7b-hf')
    parser.add_argument('-seed', '--seed', type=int, help='42', default=42)
    parser.add_argument('-l', '--layers', type=str, help='2;10;18;26')
    parser.add_argument('-r', '--rank', type=int, help=8, default=8)
    parser.add_argument('-p', '--position', type=str, help='last')
    parser.add_argument('-e', '--epochs', type=int, help='1', default=1)
    parser.add_argument('-is_wandb', '--is_wandb', type=bool, default=False)
    parser.add_argument('-save_model', '--save_model', type=bool, default=False)
    parser.add_argument('-max_n_train_example', '--max_n_train_example', type=int, default=None)
    parser.add_argument('-max_n_eval_example', '--max_n_eval_example', type=int, default=None)
    parser.add_argument(
        '-type', '--intervention_type', type=str, 
        help='LearnedSourceLowRankRotatedSpaceIntervention', default="LearnedSourceLowRankRotatedSpaceIntervention")
    parser.add_argument('-gradient_accumulation_steps', '--gradient_accumulation_steps', type=int, default=4)
    parser.add_argument('-batch_size', '--batch_size', type=int, default=4)
    parser.add_argument('-eval_batch_size', '--eval_batch_size', type=int, default=4)
    parser.add_argument('-output_dir', '--output_dir', type=str, default="./official_results")
    parser.add_argument('-lr', '--lr', type=float, default=5e-3)
    
    args = parser.parse_args()

    model = args.model
    layers = args.layers
    rank = args.rank
    position = args.position
    epochs = args.epochs
    seed = args.seed
    intervention_type = args.intervention_type
    set_seed(seed)
    max_n_train_example = args.max_n_train_example
    max_n_eval_example = args.max_n_eval_example
    is_wandb = args.is_wandb
    gradient_accumulation_steps = args.gradient_accumulation_steps
    batch_size = args.batch_size
    output_dir = args.output_dir
    task = args.task
    lr = args.lr
    train_dataset = args.train_dataset
    eval_dataset = args.eval_dataset
    save_model = args.save_model
    eval_batch_size = args.eval_batch_size
    
    assert task in {"commonsense", "math", "alpaca", "instruct"}
    
    print(
        f"task: {task}, model: {model}, intervention_type: {intervention_type}"
        f"layers: {layers}, rank: {rank}, "
        f"position: {position}, epoch: {epochs}"
    )
    model_str = model.split("/")[-1]
    run_name = f"{model_str}.{task}.intervention_type={intervention_type}.layers={layers}."\
               f"rank={rank}.position={position}.epoch={epochs}.lr={lr}"
    
    config, _, llama = create_llama(model)
    _ = llama.to(device)
    _ = llama.eval()
    
    # post-processing the inputs
    if intervention_type == "LearnedSourceLowRankRotatedSpaceIntervention":
        intervention_type = LearnedSourceLowRankRotatedSpaceIntervention
    elif intervention_type == "ConditionedSourceLowRankRotatedSpaceIntervention":
        intervention_type = ConditionedSourceLowRankRotatedSpaceIntervention
    elif intervention_type == "ConditionedSourceLowRankIntervention":
        intervention_type = ConditionedSourceLowRankIntervention
        
    user_give_all_layers = False
    if layers != "all":
        if "+" in layers:
            parsed_layers = []
            for l in layers.split("+"):
                for ll in l.split(";"):
                    parsed_layers += [int(ll)]
            user_give_all_layers = True
            layers = parsed_layers
        else:
            layers = [int(l) for l in layers.split(";")]
    else:
        layers = [l for l in range(config.num_hidden_layers)]
    assert position in {"last", "first+last"}
    if position in {"first+last"}:
        if user_give_all_layers:
            pass
        else:
            layers += layers
    
    tokenizer = AutoTokenizer.from_pretrained(model)
    tokenizer.padding_side = "right" # we will use right padding for training with teacher-forcing
    special_tokens_dict = dict()
    
    # we dont add new tokens, uncomment at your own risk.
    # if tokenizer.pad_token is None:
    #     special_tokens_dict["pad_token"] = DEFAULT_PAD_TOKEN
    # if tokenizer.eos_token is None:
    #     special_tokens_dict["eos_token"] = DEFAULT_EOS_TOKEN
    # if tokenizer.bos_token is None:
    #     special_tokens_dict["bos_token"] = DEFAULT_BOS_TOKEN
    # if tokenizer.unk_token is None:
    #     special_tokens_dict["unk_token"] = DEFAULT_UNK_TOKEN
    # num_new_tokens = tokenizer.add_special_tokens(special_tokens_dict)
    # llama.resize_token_embeddings(len(tokenizer))
    # print("adding new tokens count: ", num_new_tokens)
    tokenizer.pad_token = tokenizer.unk_token
    
    if task == "commonsense":
        max_length = 512
        train_datasets = [
            "boolq", "piqa", "social_i_qa", "hellaswag", 
            "winogrande", "ARC-Easy", "ARC-Challenge", "openbookqa"
        ] if train_dataset is None else [train_dataset]
        eval_datasets = [
            "boolq", "piqa", "social_i_qa", "hellaswag", "winogrande", "ARC-Easy", "ARC-Challenge", "openbookqa"
        ] if eval_dataset is None else [eval_dataset]
        task_prompt_template = "%s\n"
        trigger_tokens = "the correct answer is "
    elif task == "math":
        max_length = 512
        train_datasets = [
            "math_10k"
        ] if train_dataset is None else [train_dataset]
        eval_datasets = [
            "MultiArith", "gsm8k", "SVAMP", "mawps", "AddSub", "AQuA", "SingleEq", 
        ] if eval_dataset is None else [eval_dataset]
        task_prompt_template = alpaca_prompt_no_input_template
        trigger_tokens = "### Response:\n"
    elif task == "alpaca":
        max_length = 512
        train_datasets = [
            "alpaca_data_cleaned"
        ]
        task_prompt_template = alpaca_prompt_template
        trigger_tokens = "### Response:\n"
    elif task == "instruct":
        max_length = 2048
        train_datasets = [
            "instruct"
        ]
        task_prompt_template = alpaca_prompt_template
        trigger_tokens = "### Response:\n"
        
    ###################
    # data loaders
    ###################
    all_base_input_ids, all_base_positions, all_output_ids, all_source_input_ids = [], [], [], []

    for dataset in train_datasets:
        print("loading data for dataset: ", dataset)
        task_dataset = jload(f"./datasets/{dataset}/train.json")
        for data_item in task_dataset[:max_n_train_example]:
            if task == "commonsense":
                base_prompt = task_prompt_template % (data_item['instruction'])
                base_input = base_prompt + trigger_tokens + data_item["answer"] + tokenizer.eos_token
            elif task == "math":
                # we strip since these are model generated examples.
                base_prompt = task_prompt_template % (data_item['instruction'])
                base_input = base_prompt + data_item["output"] + tokenizer.eos_token
            elif task == "alpaca" or task == "instruct":
                if data_item['input'] == "":
                    base_prompt = alpaca_prompt_no_input_template % (data_item['instruction'])
                    base_input = base_prompt + data_item["output"] + tokenizer.eos_token
                else:
                    base_prompt = task_prompt_template % (data_item['instruction'], data_item['input'])
                    base_input = base_prompt + data_item["output"] + tokenizer.eos_token
            base_prompt_length = len(tokenizer(
                base_prompt, max_length=max_length, truncation=True, return_tensors="pt")["input_ids"][0])
            base_input_ids = tokenizer(
                base_input, max_length=max_length, truncation=True, return_tensors="pt")["input_ids"][0]
            output_ids = tokenizer(
                base_input, max_length=max_length, truncation=True, return_tensors="pt")["input_ids"][0]
            output_ids[:base_prompt_length] = -100
            base_input_ids[-1] = tokenizer.eos_token_id # enforce the last token to be eos
            output_ids[-1] = tokenizer.eos_token_id # enforce the last token to be eos
        
            all_base_input_ids.append(base_input_ids)
            all_base_positions.append([base_prompt_length-1]) # intervene on the last prompt token
            all_output_ids.append(output_ids)
            
    raw_train = (
        all_base_input_ids,
        all_base_positions,
        all_output_ids,
    )
    
    train_dataset = Dataset.from_dict(
        {
            "input_ids": raw_train[0],
            "intervention_position": raw_train[1],
            "labels": raw_train[2],
        }
    ).shuffle(seed=seed)
    if max_n_train_example is not None:
        train_dataset = train_dataset.select(range(max_n_train_example))
    
    data_collator = DataCollatorForSeq2Seq(
        tokenizer=tokenizer,
        model=llama,
        label_pad_token_id=-100,
        padding="longest"
    )
    
    initial_lr = lr
    total_step = 0
    train_dataloader = DataLoader(
        train_dataset, shuffle=True, batch_size=batch_size, collate_fn=data_collator)
    t_total = int(len(train_dataloader) * epochs) // gradient_accumulation_steps

    config = IntervenableConfig([{
        "layer": l,
        "component": "block_output",
        "low_rank_dimension": rank} for l in layers],
        intervention_type
    )
    intervenable = IntervenableModel(config, llama)
    intervenable.set_device(device)
    intervenable.disable_model_gradients()
    
    n_params = intervenable.count_parameters()
    
    if intervention_type == "ConditionedSourceLowRankIntervention":
        optimizer = torch.optim.AdamW(
            intervenable.get_trainable_parameters(), lr=initial_lr
        )
    else:
        optimizer = torch.optim.Adam(
            intervenable.get_trainable_parameters(), lr=initial_lr
        )
    
    scheduler = get_linear_schedule_with_warmup(
        optimizer, 
        num_warmup_steps=20 if task == "math" else 100, 
        num_training_steps=t_total
    )

    intervenable.model.train()  # train enables drop-off but no grads
    print("llama trainable parameters: ", count_parameters(intervenable.model))
    print("intervention trainable parameters: ", n_params)
    
    if is_wandb:
        run = wandb.init(
            project=f"Steer_LM_{task}", 
            entity="wuzhengx",
            name=run_name,
        )
        wandb.log({"train/n_params": n_params})
                
    train_iterator = trange(0, int(epochs), desc="Epoch")
    for epoch in train_iterator:
        epoch_iterator = tqdm(
            train_dataloader, desc=f"Epoch: {epoch}", position=0, leave=True
        )
        for step, inputs in enumerate(epoch_iterator):
            for k, v in inputs.items():
                if v is not None and isinstance(v, torch.Tensor):
                    inputs[k] = v.to(device)
            b_s = inputs["input_ids"].shape[0]
            base_unit_location = inputs["intervention_position"].tolist()
            if position in {"first+last"}:
                base_first_token = torch.zeros_like(inputs["intervention_position"]).tolist()
                intervention_locations = [base_first_token]*(len(layers)//2)+[base_unit_location]*(len(layers)//2)
            else:
                intervention_locations = [base_unit_location]*len(layers)
                
            _, cf_outputs = intervenable(
                {"input_ids": inputs["input_ids"]},
                unit_locations={"sources->base": (None, intervention_locations)})

            # lm loss on counterfactual labels
            lm_logits = cf_outputs.logits
            labels = inputs["labels"]
            shift_logits = lm_logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            # Flatten the tokens
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))
            loss_str = round(loss.item(), 2)
            epoch_iterator.set_postfix({"loss": loss_str})
            if is_wandb:
                wandb.log({"train/loss": loss_str})
            if gradient_accumulation_steps > 1:
                loss = loss / gradient_accumulation_steps
            loss.backward()
            if total_step % gradient_accumulation_steps == 0:
                if not (gradient_accumulation_steps > 1 and total_step == 0):
                    optimizer.step()
                    scheduler.step()
                    optimizer.zero_grad()
            total_step += 1
    
    if save_model:
        intervenable.save(save_directory=f"{output_dir}/{run_name}")
    create_directory(f"{output_dir}/{run_name}")
    
    args_dict = vars(args)
    args_dict["n_params"] = n_params
    json_file_name = f"{output_dir}/{run_name}/args.json"
    with open(json_file_name, 'w') as json_file:
        json.dump(args_dict, json_file, indent=4)
    
    # ensure everything is in eval mode
    intervenable.model.eval()
    for k,v in intervenable.interventions.items():
        _ = v[0].eval()
    tokenizer.padding_side = "left" # switch padding side for generation
    
    if task == "commonsense":
        eval_results = {}
        for i in range(len(eval_datasets)):
            eval_dataset = jload(f"./datasets/{eval_datasets[i]}/test.json")
            if max_n_eval_example is not None:
                max_sample_examples = min(max_n_eval_example, len(eval_dataset))
                sampled_items = random.sample(range(len(eval_dataset)), max_sample_examples)
                sampled_eval_dataset = [eval_dataset[idx] for idx in sampled_items]
            else:
                sampled_eval_dataset = eval_dataset
            correct_count = 0
            totol_count = 0
            eval_iterator = tqdm(
                chunk(sampled_eval_dataset, eval_batch_size), position=0, leave=True
            )
            generations = []
            with torch.no_grad():
                for batch_example in eval_iterator:
                    
                    actual_batch = []
                    for _, example in enumerate(batch_example):
                        prompt = task_prompt_template % (example['instruction'])
                        actual_batch.append(prompt)
                    
                    # tokenize in batch
                    tokenized = tokenizer.batch_encode_plus(
                        actual_batch, return_tensors='pt', padding=True).to(device)
                    batch_length = tokenized["attention_mask"].sum(dim=-1).tolist()
                    base_last_unit_location = tokenized["input_ids"].shape[-1] - 1 
                    base_last_unit_location = [[base_last_unit_location]]*len(batch_example)
                    base_first_unit_location = [[
                        tokenized["input_ids"].shape[-1] - batch_length[i]] 
                        for i in range(len(batch_example))]

                    if position in {"first+last"}:
                        base_unit_location = tokenized["input_ids"].shape[-1] - 1 
                        base_unit_location = {"sources->base": (
                            None, [base_first_unit_location]*(len(layers)//2) + 
                            [base_last_unit_location]*(len(layers)//2))}
                    else:
                        base_unit_location = tokenized["input_ids"].shape[-1] - 1 
                        base_unit_location = {"sources->base": (None, [base_last_unit_location])}

                    _, steered_response = intervenable.generate(
                        tokenized, 
                        unit_locations=base_unit_location,
                        intervene_on_prompt=True,
                        max_new_tokens=10, do_sample=False, 
                        eos_token_id=tokenizer.eos_token_id, early_stopping=True
                    )
                    
                    # detokenize in batch
                    actual_preds = tokenizer.batch_decode(steered_response, skip_special_tokens=True)
                    for in_text, pred, example in zip(actual_batch, actual_preds, batch_example):
                        try:
                            generation = extract_output(pred, trigger_tokens)
                        except:
                            print("get not split based on trigger tokens: ", raw_generation)
                            generation = "WRONG"
                        answer = example["answer"]
                        if generation.strip() == answer.strip():
                            correct_count += 1
                        totol_count += 1
                        metric_str = round(correct_count/totol_count, 3)
                        eval_iterator.set_postfix({"em": metric_str})
                        generations += [{
                            "instruction": example["instruction"],
                            "generation": generation,
                            "answer": answer
                        }]
            metric_str = round(correct_count/totol_count, 3)
            if is_wandb:
                wandb.log({f"eval/{eval_datasets[i]}": metric_str})
            eval_results[eval_datasets[i]] = metric_str
            result_json_file_name = f"{output_dir}/{run_name}/{eval_datasets[i]}_outputs.json"
            with open(result_json_file_name, 'w') as json_file:
                json.dump(generations, json_file, indent=4)

        result_json_file_name = f"{output_dir}/{run_name}/eval_results.json"
        eval_results["n_params"] = n_params
        with open(result_json_file_name, 'w') as json_file:
            json.dump(eval_results, json_file, indent=4)
            
    elif task == "math":
        eval_results = {}
        for i in range(len(eval_datasets)):
            eval_dataset = jload(f"./datasets/{eval_datasets[i]}/test.json")
            if max_n_eval_example is not None:
                max_sample_examples = min(max_n_eval_example, len(eval_dataset))
                sampled_items = random.sample(range(len(eval_dataset)), max_sample_examples)
                sampled_eval_dataset = [eval_dataset[idx] for idx in sampled_items]
            else:
                sampled_eval_dataset = eval_dataset
            correct_count = 0
            totol_count = 0
            eval_iterator = tqdm(
                chunk(sampled_eval_dataset, eval_batch_size), position=0, leave=True
            )
            generations = []
            with torch.no_grad():
                for batch_example in eval_iterator:
                    
                    actual_batch = []
                    for _, example in enumerate(batch_example):
                        prompt = task_prompt_template % (example['instruction'])
                        actual_batch.append(prompt)
                    
                    # tokenize in batch
                    tokenized = tokenizer.batch_encode_plus(
                        actual_batch, return_tensors='pt', padding=True).to(device)
                    batch_length = tokenized["attention_mask"].sum(dim=-1).tolist()
                    base_last_unit_location = tokenized["input_ids"].shape[-1] - 1 
                    base_last_unit_location = [[base_last_unit_location]]*len(batch_example)
                    base_first_unit_location = [[
                        tokenized["input_ids"].shape[-1] - batch_length[i]] 
                        for i in range(len(batch_example))]
                    
                    if position in {"first+last"}:
                        base_unit_location = tokenized["input_ids"].shape[-1] - 1 
                        base_unit_location = {"sources->base": (
                            None, [base_first_unit_location]*(len(layers)//2) + 
                            [base_last_unit_location]*(len(layers)//2))}
                    else:
                        base_unit_location = tokenized["input_ids"].shape[-1] - 1 
                        base_unit_location = {"sources->base": (None, [base_last_unit_location])}

                    _, steered_response = intervenable.generate(
                        tokenized, 
                        unit_locations=base_unit_location,
                        intervene_on_prompt=True,
                        # since we are generating the CoT, we follow previous works setting.
                        max_new_tokens=256,
                        temperature=0.1,
                        top_p=0.75,
                        top_k=40,
                        num_beams=1,
                        do_sample=True,
                        eos_token_id=tokenizer.eos_token_id, 
                        early_stopping=True,
                    )
                    
                    # detokenize in batch
                    actual_preds = tokenizer.batch_decode(steered_response, skip_special_tokens=True)
                    for in_text, pred, example in zip(actual_batch, actual_preds, batch_example):
                        answer = example["answer"].strip()
                        try:
                            raw_generation = extract_output(pred, trigger_tokens)
                        except:
                            print("get not split based on trigger tokens: ", raw_generation)
                            raw_generation = "WRONG"

                        if eval_datasets[i] == "AQuA":
                            generation = extract_answer_letter(raw_generation)
                            if generation.strip() == answer.strip():
                                correct_count += 1
                        else:
                            generation = extract_answer_number(raw_generation)
                            if generation == float(answer):
                                correct_count += 1
                        totol_count += 1
                        metric_str = round(correct_count/totol_count, 3)
                        eval_iterator.set_postfix({"em": metric_str})
                        generations += [{
                            "instruction": example["instruction"],
                            "raw_generation": raw_generation,
                            "generation": generation,
                            "answer": answer
                        }]
                        
            metric_str = round(correct_count/totol_count, 3)
            if is_wandb:
                wandb.log({f"eval/{eval_datasets[i]}": metric_str})
            eval_results[eval_datasets[i]] = metric_str
            result_json_file_name = f"{output_dir}/{run_name}/{eval_datasets[i]}_outputs.json"
            with open(result_json_file_name, 'w') as json_file:
                json.dump(generations, json_file, indent=4)

        result_json_file_name = f"{output_dir}/{run_name}/eval_results.json"
        eval_results["n_params"] = n_params
        with open(result_json_file_name, 'w') as json_file:
            json.dump(eval_results, json_file, indent=4)
            
            
    elif task == "alpaca" or task == "instruct":
        eval_dataset = load_dataset("tatsu-lab/alpaca_eval", "alpaca_eval")["eval"]
        generations = []
        idx = 0
        with torch.no_grad():
            for batch_example in tqdm(chunk(eval_dataset, eval_batch_size)):
                
                actual_batch = []
                for _, example in enumerate(batch_example):
                    q = example["instruction"]
                    prompt = alpaca_prompt_no_input_template % q
                    actual_batch.append(prompt)
                
                # tokenize in batch
                tokenized = tokenizer.batch_encode_plus(
                    actual_batch, return_tensors='pt', padding=True).to(device)
                batch_length = tokenized["attention_mask"].sum(dim=-1).tolist()
                base_last_unit_location = tokenized["input_ids"].shape[-1] - 1 
                base_last_unit_location = [[base_last_unit_location]]*len(batch_example)
                base_first_unit_location = [[
                    tokenized["input_ids"].shape[-1] - batch_length[i]] 
                    for i in range(len(batch_example))]

                if position in {"first+last"}:
                    base_unit_location = tokenized["input_ids"].shape[-1] - 1 
                    base_unit_location = {"sources->base": (
                        None, [base_first_unit_location]*(len(layers)//2) + 
                        [base_last_unit_location]*(len(layers)//2))}
                else:
                    base_unit_location = tokenized["input_ids"].shape[-1] - 1 
                    base_unit_location = {"sources->base": (None, [base_last_unit_location])}

                _, steered_response = intervenable.generate(
                    tokenized, 
                    unit_locations=base_unit_location,
                    intervene_on_prompt=True,
                    # much longer generation
                    max_new_tokens=2048,
                    temperature=0.7,
                    top_p=1.0,
                    do_sample=True,
                    eos_token_id=tokenizer.eos_token_id, 
                    early_stopping=True
                )

                # detokenize in batch
                actual_preds = tokenizer.batch_decode(steered_response, skip_special_tokens=True)
                for in_text, pred, example in zip(actual_batch, actual_preds, batch_example):
                    generation = extract_output(pred, trigger_tokens)
                    generations += [{
                        "instruction": example["instruction"],
                        "output": generation,
                        "dataset": example["dataset"],
                        "generator": run_name
                    }]
                    idx += 1
                    if max_n_eval_example is not None:
                        if idx >= max_n_eval_example:
                            break

        result_json_file_name = f"{output_dir}/{run_name}/outputs.json"
        with open(result_json_file_name, 'w') as json_file:
            json.dump(generations, json_file, indent=4)
        
if __name__ == "__main__":
    main()