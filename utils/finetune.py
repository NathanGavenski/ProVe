#!/usr/bin/env python

import argparse
import glob
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple
import pdb

import numpy as np
import pytorch_lightning as pl
import torch
from torch.utils.data import DataLoader

from pytorch_lightning.utilities import rank_zero_info

from utils.callbacks import Seq2SeqLoggingCallback, get_checkpoint_callback, get_early_stopping_callback
from transformers import MBartTokenizer, T5ForConditionalGeneration

from transformers.models.bart.modeling_bart import shift_tokens_right
from utils.utils_verbalisation_module import (
    ROUGE_KEYS,
    LegacySeq2SeqDataset,
    Seq2SeqDataset,
    assert_all_frozen,
    calculate_bleu,
    calculate_rouge,
    flatten_list,
    freeze_embeds,
    freeze_params,
    label_smoothed_nll_loss,
    lmap,
    pickle_save,
    save_json,
    use_task_specific_params,
)

from utils.utils_graph2text import convert_text, eval_meteor, eval_bleu, eval_chrf, eval_meteor_test_webnlg, eval_chrf_test_webnlg

# need the parent dir module
sys.path.insert(2, str(Path(__file__).resolve().parents[1]))
from utils.lightning_base import BaseTransformer, add_generic_args, generic_train  # noqa


logger = logging.getLogger(__name__)


class SummarizationModule(BaseTransformer):
    mode = "summarization"
    loss_names = ["loss"]
    metric_names = ROUGE_KEYS
    default_val_metric = "rouge2"

    def __init__(self, hparams, **kwargs):
        if hparams.sortish_sampler and hparams.gpus > 1:
            hparams.replace_sampler_ddp = False
        elif hparams.max_tokens_per_batch is not None:
            if hparams.gpus > 1:
                raise NotImplementedError("Dynamic Batch size does not work for multi-gpu training")
            if hparams.sortish_sampler:
                raise ValueError("--sortish_sampler and --max_tokens_per_batch may not be used simultaneously")
                
        super().__init__(hparams, num_labels=None, mode=self.mode, **kwargs)
        #use_task_specific_params(self.model, "summarization")

        self.metrics_save_path = Path('/home/ubuntu/RQV/base') / "metrics.json"
        self.hparams_save_path = Path('/home/ubuntu/RQV/base') / "hparams.pkl"
        pickle_save(self.hparams, self.hparams_save_path)
        self.step_count = -2
        self.metrics = defaultdict(list)
        self.model_type = self.config.model_type
        self.vocab_size = self.config.tgt_vocab_size if self.model_type == "fsmt" else self.config.vocab_size

        if 't5' in hparams.model_name_or_path:
            self.model.config.prefix = 'translate Graph to English: '
        self.dataset_kwargs: dict = dict(
            data_dir=self.hparams.data_dir,
            max_source_length=self.hparams.max_source_length,
            prefix=self.model.config.prefix or "",
        )
        n_observations_per_split = {
            "train": self.hparams.n_train,
            "val": self.hparams.n_val,
            "test_seen": self.hparams.n_test,
            "test_unseen": self.hparams.n_test,
            "test_both": self.hparams.n_test,
        }
        self.n_obs = {k: v if v >= 0 else None for k, v in n_observations_per_split.items()}

        self.target_lens = {
            "train": self.hparams.max_target_length,
            "val": self.hparams.val_max_target_length,
            "test_seen": self.hparams.test_max_target_length,
            "test_unseen": self.hparams.test_max_target_length,
            "test_both": self.hparams.test_max_target_length,
        }
        assert self.target_lens["train"] <= self.target_lens["val"], f"target_lens: {self.target_lens}"
        assert self.target_lens["train"] <= self.target_lens["test_both"], f"target_lens: {self.target_lens}"
        if self.hparams.freeze_embeds:
            freeze_embeds(self.model)
        if self.hparams.freeze_encoder:
            freeze_params(self.model.get_encoder())
            assert_all_frozen(self.model.get_encoder())

        self.num_workers = hparams.num_workers
        self.decoder_start_token_id = None  # default to config
        if self.model.config.decoder_start_token_id is None and isinstance(self.tokenizer, MBartTokenizer):
            self.decoder_start_token_id = self.tokenizer.lang_code_to_id[hparams.tgt_lang]
            self.model.config.decoder_start_token_id = self.decoder_start_token_id
        self.dataset_class = (
            Seq2SeqDataset if hasattr(self.tokenizer, "prepare_seq2seq_batch") else LegacySeq2SeqDataset
        )
        self.already_saved_batch = False
        self.eval_beams = self.model.config.num_beams if self.hparams.eval_beams is None else self.hparams.eval_beams
        if self.hparams.eval_max_gen_length is not None:
            self.eval_max_length = self.hparams.eval_max_gen_length
        else:
            self.eval_max_length = self.model.config.max_length
        self.val_metric = self.default_val_metric if self.hparams.val_metric is None else self.hparams.val_metric

    def save_readable_batch(self, batch: Dict[str, torch.Tensor]) -> Dict[str, List[str]]:
        """A debugging utility"""

        readable_batch = {
            k: self.tokenizer.batch_decode(v.tolist()) if "mask" not in k else v.shape for k, v in batch.items()
        }
        save_json(readable_batch, Path(self.output_dir) / "text_batch.json")

        tb = {}
        for k, v in batch.items():
            tb[k] = v.tolist()

        save_json(tb, Path(self.output_dir) / "tok_batch.json")

        self.already_saved_batch = True
        return readable_batch

    def forward(self, input_ids, **kwargs):
        return self.model(input_ids, **kwargs)

    def ids_to_clean_text(self, generated_ids: List[int]):
        gen_text = self.tokenizer.batch_decode(
            generated_ids, skip_special_tokens=True, clean_up_tokenization_spaces=True
        )
        return lmap(str.strip, gen_text)

    def _step(self, batch: dict) -> Tuple:
        pad_token_id = self.tokenizer.pad_token_id
        src_ids, src_mask = batch["input_ids"], batch["attention_mask"]
        if isinstance(self.model, T5ForConditionalGeneration):
            tgt_ids = batch["labels"]
            decoder_input_ids = self.model._shift_right(tgt_ids)
        else:
            #decoder_input_ids = shift_tokens_right(tgt_ids, pad_token_id)
            y = batch["labels"]
            decoder_input_ids = y[:, :-1].contiguous()
            tgt_ids = y[:, 1:].clone()
        if not self.already_saved_batch:  # This would be slightly better if it only happened on rank zero
            batch["decoder_input_ids"] = decoder_input_ids
            self.save_readable_batch(batch)

        outputs = self(src_ids, attention_mask=src_mask, decoder_input_ids=decoder_input_ids, use_cache=False)
        lm_logits = outputs[0]
        if self.hparams.label_smoothing == 0:
            # Same behavior as modeling_bart.py, besides ignoring pad_token_id
            ce_loss_fct = torch.nn.CrossEntropyLoss(ignore_index=pad_token_id)

            assert lm_logits.shape[-1] == self.vocab_size
            loss = ce_loss_fct(lm_logits.view(-1, lm_logits.shape[-1]), tgt_ids.view(-1))
        else:
            lprobs = torch.nn.functional.log_softmax(lm_logits, dim=-1)
            loss, nll_loss = label_smoothed_nll_loss(
                lprobs, tgt_ids, self.hparams.label_smoothing, ignore_index=pad_token_id
            )
        return (loss,)

    @property
    def pad(self) -> int:
        return self.tokenizer.pad_token_id

    def training_step(self, batch, batch_idx) -> Dict:
        loss_tensors = self._step(batch)

        logs = {name: loss for name, loss in zip(self.loss_names, loss_tensors)}
        # tokens per batch
        logs["tpb"] = batch["input_ids"].ne(self.pad).sum() + batch["labels"].ne(self.pad).sum()
        logs["bs"] = batch["input_ids"].shape[0]
        logs["src_pad_tok"] = batch["input_ids"].eq(self.pad).sum()
        logs["src_pad_frac"] = batch["input_ids"].eq(self.pad).float().mean()
        # TODO(SS): make a wandb summary metric for this
        return {"loss": loss_tensors[0], "log": logs}

    def validation_step(self, batch, batch_idx) -> Dict:
        return self._generative_step(batch)

    def validation_epoch_end(self, outputs, prefix="val") -> Dict:

        self.step_count += 1

        val_outputs_folder = "val_outputs"
        os.system("mkdir -p " + os.path.join(self.hparams.output_dir, val_outputs_folder))

        if prefix == "val":
            output_test_predictions_file = os.path.join(self.hparams.output_dir, val_outputs_folder, "validation_predictions_" +
                                                        str(self.step_count) + ".txt")
            output_test_targets_file = os.path.join(self.hparams.output_dir, val_outputs_folder, "validation_targets_" +
                                                        str(self.step_count) + ".txt")
            # write predictions and targets for later rouge evaluation.
            with open(output_test_predictions_file, "w") as p_writer, open(output_test_targets_file, "w") as t_writer:
                for output_batch in outputs:
                    p_writer.writelines(convert_text(s) + "\n" for s in output_batch["preds"])
                    t_writer.writelines(convert_text(s) + "\n" for s in output_batch["target"])
                p_writer.close()
                t_writer.close()

            bleu_info = eval_bleu(self.hparams.data_dir, output_test_predictions_file, 'val')

            rank_zero_info("%s bleu_info: %s", self.step_count, bleu_info)

            if bleu_info == -1:
                bleu_info = float(bleu_info)
            else:
                bleu_info = float(bleu_info.split(",")[0].split("BLEU = ")[1])

            losses = {k: torch.stack([x[k] for x in outputs]).mean() for k in self.loss_names}
            loss = losses["loss"]
            generative_metrics = {
                k: np.array([x[k] for x in outputs]).mean() for k in self.metric_names + ["gen_time", "gen_len"]
            }

            generative_metrics['bleu'] = bleu_info

            metric_val = (
                generative_metrics[self.val_metric] if self.val_metric in generative_metrics else losses[
                    self.val_metric]
            )
            metric_tensor: torch.FloatTensor = torch.tensor(metric_val).type_as(loss)
            generative_metrics.update({k: v.item() for k, v in losses.items()})
            losses.update(generative_metrics)
            all_metrics = {f"{prefix}_avg_{k}": x for k, x in losses.items()}
            all_metrics["step_count"] = self.step_count
            self.metrics[prefix].append(all_metrics)  # callback writes this to self.metrics_save_path
            preds = flatten_list([x["preds"] for x in outputs])

            return {
                "bleu": bleu_info,
                "log": all_metrics,
                "preds": preds,
                f"{prefix}_loss": loss,
                f"{prefix}_{self.val_metric}": metric_tensor,
            }
        else:

            data_logs = {}
            for output in outputs:

                dataset_idx = output[0]['dataloader_idx']

                if dataset_idx == 0:
                    dataset_name = 'test_both'
                elif dataset_idx == 1:
                    dataset_name = 'test_seen'
                else:
                    dataset_name = 'test_unseen'

                if output[0]['bleu'] == -1:
                    bleu_info = float(output[0]['bleu'])
                else:
                    bleu_info = float(output[0]['bleu'].split(",")[0].split("BLEU = ")[1])


                losses = {k: torch.stack([x[k] for x in output]).mean() for k in self.loss_names}
                loss = losses["loss"]
                generative_metrics = {
                    k: np.array([x[k] for x in output]).mean() for k in self.metric_names + ["gen_time", "gen_len"]
                }

                generative_metrics['bleu'] = bleu_info

                metric_val = (
                    generative_metrics[self.val_metric] if self.val_metric in generative_metrics else losses[
                        self.val_metric]
                )
                metric_tensor: torch.FloatTensor = torch.tensor(metric_val).type_as(loss)
                generative_metrics.update({k: v.item() for k, v in losses.items()})
                losses.update(generative_metrics)
                all_metrics = {f"{prefix}_avg_{k}": x for k, x in losses.items()}
                all_metrics["step_count"] = self.step_count
                self.metrics[prefix].append(all_metrics)  # callback writes this to self.metrics_save_path
                preds = flatten_list([x["preds"] for x in output])

                data_logs.update({
                    "log" + "_" + dataset_name: all_metrics,
                    "preds" + "_" + dataset_name: preds,
                    f"{prefix}_loss" + "_" + dataset_name: loss,
                    f"{prefix}_{self.val_metric}" + "_" + dataset_name: metric_tensor,
                })
            return data_logs


        #######




    def calc_generative_metrics(self, preds, target) -> Dict:
        return calculate_rouge(preds, target)

    def _generative_step(self, batch: dict, batch_idx=None, dataloader_idx=None) -> dict:
        t0 = time.time()

        # parser.add_argument('--eval_max_gen_length', type=int, default=None, help='never generate more than n tokens')
        generated_ids = self.model.generate(
            batch["input_ids"],
            attention_mask=batch["attention_mask"],
            use_cache=True,
            decoder_start_token_id=self.decoder_start_token_id,
            num_beams=self.eval_beams,
            max_length=self.eval_max_length,
            length_penalty=1.0
        )
        gen_time = (time.time() - t0) / batch["input_ids"].shape[0]
        preds: List[str] = self.ids_to_clean_text(generated_ids)
        target: List[str] = self.ids_to_clean_text(batch["labels"])
        loss_tensors = self._step(batch)
        base_metrics = {name: loss for name, loss in zip(self.loss_names, loss_tensors)}
        rouge: Dict = self.calc_generative_metrics(preds, target)
        summ_len = np.mean(lmap(len, generated_ids))
        base_metrics.update(gen_time=gen_time, gen_len=summ_len, preds=preds, target=target, **rouge)

        if dataloader_idx is not None:
            base_metrics.update(batch_idx=batch_idx, dataloader_idx=dataloader_idx)
        return base_metrics

    def test_step(self, batch, batch_idx, dataloader_idx):
        return self._generative_step(batch, batch_idx, dataloader_idx)

    def test_epoch_end(self, outputs_all_testsets):

        val_outputs_folder = "val_outputs"
        os.system("mkdir -p " + os.path.join(self.hparams.output_dir, val_outputs_folder))

        for outputs in outputs_all_testsets:
            dataset_idx = outputs[0]['dataloader_idx']

            if dataset_idx == 0:
                file_name = "test_both_predictions.txt"
                file_name_tgt = "test_both_targets.txt"
                dataset_name = 'test_both'
            elif dataset_idx == 1:
                file_name = "test_seen_predictions.txt"
                file_name_tgt = "test_seen_targets.txt"
                dataset_name = 'test_seen'
            else:
                file_name = "test_unseen_predictions.txt"
                file_name_tgt = "test_unseen_targets.txt"
                dataset_name = 'test_unseen'

            file_name += '.debug'
            file_name_tgt += '.debug'

            output_test_predictions_file = os.path.join(self.hparams.output_dir, val_outputs_folder, file_name)
            output_test_targets_file = os.path.join(self.hparams.output_dir, val_outputs_folder, file_name_tgt)
            # write predictions and targets for later rouge evaluation.
            with open(output_test_predictions_file, "w") as p_writer, open(output_test_targets_file, "w") as t_writer:
                for output_batch in outputs:

                    p_writer.writelines(convert_text(s) + "\n" for s in output_batch["preds"])
                    t_writer.writelines(convert_text(s) + "\n" for s in output_batch["target"])
                p_writer.close()
                t_writer.close()

            bleu_info = eval_bleu(self.hparams.data_dir, output_test_predictions_file, dataset_name)
            meteor_info = eval_meteor_test_webnlg(self.hparams.data_dir, output_test_predictions_file, dataset_name)
            chrf_info = eval_chrf_test_webnlg(self.hparams.data_dir, output_test_predictions_file, dataset_name)

            rank_zero_info(" %s - bleu_info: %s", dataset_name, bleu_info)
            rank_zero_info(" %s - meteor_info: %s", dataset_name, meteor_info)
            rank_zero_info(" %s - chrf_info: %s", dataset_name, chrf_info)

            outputs[0]['bleu'] = bleu_info

        return self.validation_epoch_end(outputs_all_testsets, prefix="test")

    def get_dataset(self, type_path) -> Seq2SeqDataset:
        n_obs = self.n_obs[type_path]
        max_target_length = self.target_lens[type_path]
        dataset = self.dataset_class(
            self.tokenizer,
            type_path=type_path,
            n_obs=n_obs,
            max_target_length=max_target_length,
            **self.dataset_kwargs,
        )
        return dataset

    def get_dataloader(self, type_path: str, batch_size: int, shuffle: bool = False) -> DataLoader:
        dataset = self.get_dataset(type_path)

        if self.hparams.sortish_sampler and type_path != "test":
            sampler = dataset.make_sortish_sampler(batch_size, distributed=self.hparams.gpus > 1)
            return DataLoader(
                dataset,
                batch_size=batch_size,
                collate_fn=dataset.collate_fn,
                shuffle=False,
                num_workers=self.num_workers,
                sampler=sampler,
            )

        elif self.hparams.max_tokens_per_batch is not None and type_path != "test":
            batch_sampler = dataset.make_dynamic_sampler(
                self.hparams.max_tokens_per_batch, distributed=self.hparams.gpus > 1
            )
            return DataLoader(
                dataset,
                batch_sampler=batch_sampler,
                collate_fn=dataset.collate_fn,
                # shuffle=False,
                num_workers=self.num_workers,
                # batch_size=None,
            )
        else:
            return DataLoader(
                dataset,
                batch_size=batch_size,
                collate_fn=dataset.collate_fn,
                shuffle=shuffle,
                num_workers=self.num_workers,
                sampler=None,
            )

    def train_dataloader(self) -> DataLoader:
        dataloader = self.get_dataloader("train", batch_size=self.hparams.train_batch_size, shuffle=True)
        return dataloader

    def val_dataloader(self) -> DataLoader:
        return self.get_dataloader("val", batch_size=self.hparams.eval_batch_size)

    def test_dataloader(self) -> List[DataLoader]:
        test_dataloader = self.get_dataloader("test_both", batch_size=self.hparams.eval_batch_size)
        test_seen_dataloader = self.get_dataloader("test_seen", batch_size=self.hparams.eval_batch_size)
        test_unseen_dataloader = self.get_dataloader("test_unseen", batch_size=self.hparams.eval_batch_size)

        return [test_dataloader, test_seen_dataloader, test_unseen_dataloader]

    @staticmethod
    def add_model_specific_args(parser, root_dir):
        BaseTransformer.add_model_specific_args(parser, root_dir)
        add_generic_args(parser, root_dir)
        parser.add_argument(
            "--max_source_length",
            default=1024,
            type=int,
            help="The maximum total input sequence length after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded.",
        )
        parser.add_argument(
            "--max_target_length",
            default=56,
            type=int,
            help="The maximum total input sequence length after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded.",
        )
        parser.add_argument(
            "--val_max_target_length",
            default=142,  # these defaults are optimized for CNNDM. For xsum, see README.md.
            type=int,
            help="The maximum total input sequence length after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded.",
        )
        parser.add_argument(
            "--test_max_target_length",
            default=142,
            type=int,
            help="The maximum total input sequence length after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded.",
        )
        parser.add_argument("--freeze_encoder", action="store_true")
        parser.add_argument("--freeze_embeds", action="store_true")
        parser.add_argument("--sortish_sampler", action="store_true", default=False)
        parser.add_argument("--max_tokens_per_batch", type=int, default=None)
        parser.add_argument("--logger_name", type=str, choices=["default", "wandb", "wandb_shared"], default="default")
        parser.add_argument("--n_train", type=int, default=-1, required=False, help="# examples. -1 means use all.")
        parser.add_argument("--n_val", type=int, default=-1, required=False, help="# examples. -1 means use all.")
        parser.add_argument("--n_test", type=int, default=-1, required=False, help="# examples. -1 means use all.")
        parser.add_argument(
            "--task", type=str, default="summarization", required=False, help="# examples. -1 means use all."
        )
        parser.add_argument("--label_smoothing", type=float, default=0.0, required=False)
        parser.add_argument("--src_lang", type=str, default="", required=False)
        parser.add_argument("--tgt_lang", type=str, default="", required=False)
        parser.add_argument("--eval_beams", type=int, default=None, required=False)
        parser.add_argument("--checkpoint", type=str, default=None, required=False)
        parser.add_argument(
            "--val_metric", type=str, default=None, required=False, choices=["bleu", "rouge2", "loss", None]
        )
        parser.add_argument("--eval_max_gen_length", type=int, default=None, help="never generate more than n tokens")
        parser.add_argument("--save_top_k", type=int, default=1, required=False, help="How many checkpoints to save")
        parser.add_argument(
            "--early_stopping_patience",
            type=int,
            default=-1,
            required=False,
            help="-1 means never early stop. early_stopping_patience is measured in validation checks, not epochs. So val_check_interval will effect it.",
        )

        return parser


class TranslationModule(SummarizationModule):
    mode = "translation"
    loss_names = ["loss"]
    metric_names = ["bleu"]
    default_val_metric = "bleu"

    def __init__(self, hparams, **kwargs):
        super().__init__(hparams, **kwargs)
        self.dataset_kwargs["src_lang"] = hparams.src_lang
        self.dataset_kwargs["tgt_lang"] = hparams.tgt_lang

    def calc_generative_metrics(self, preds, target) -> dict:
        return calculate_bleu(preds, target)


class Graph2TextModule(SummarizationModule):
    mode = "graph2text"
    loss_names = ["loss"]
    metric_names = ["sacrebleu"]
    default_val_metric = "bleu"

    def __init__(self, hparams, **kwargs):
        if type(hparams) == dict:
            hparams = argparse.Namespace(**hparams)
        print(f'Graph2Text hparams are: {hparams}')
        super().__init__(hparams, **kwargs)

        self.hparams.update(vars(hparams))

        rank_zero_info("parameters %s", hparams)

    def calc_generative_metrics(self, preds, target) -> dict:
        return calculate_bleu(preds, target)


def main(args, model=None) -> SummarizationModule:
    Path(args.output_dir).mkdir(exist_ok=True)
    if len(os.listdir(args.output_dir)) > 3 and args.do_train:
        raise ValueError("Output directory ({}) already exists and is not empty.".format(args.output_dir))
    if model is None:
        if "summarization" in args.task:
            model: SummarizationModule = SummarizationModule(args)
        elif "translation" in args.task:
            model: SummarizationModule = TranslationModule(args)
        else:
            model: SummarizationModule = Graph2TextModule(args)
    dataset = Path(args.data_dir).name
    if (
        args.logger_name == "default"
        or args.fast_dev_run
        or str(args.output_dir).startswith("/tmp")
        or str(args.output_dir).startswith("/var")
    ):
        logger = True  # don't pollute wandb logs unnecessarily
    elif args.logger_name == "wandb":
        from pytorch_lightning.loggers import WandbLogger

        project = os.environ.get("WANDB_PROJECT", dataset)
        logger = WandbLogger(name=model.output_dir.name, project=project)

    elif args.logger_name == "wandb_shared":
        from pytorch_lightning.loggers import WandbLogger

        logger = WandbLogger(name=model.output_dir.name, project=f"hf_{dataset}")

    if args.early_stopping_patience >= 0:
        es_callback = get_early_stopping_callback(model.val_metric, args.early_stopping_patience)
    else:
        es_callback = False

    lower_is_better = args.val_metric == "loss"
    trainer: pl.Trainer = generic_train(
        model,
        args,
        logging_callback=Seq2SeqLoggingCallback(),
        checkpoint_callback=get_checkpoint_callback(
            args.output_dir, model.val_metric, args.save_top_k, lower_is_better
        ),
        early_stopping_callback=es_callback,
        logger=logger,
    )
    pickle_save(model.hparams, model.output_dir / "hparams.pkl")
    if not args.do_predict:
        return model

    model.hparams.test_checkpoint = ""
    if not args.checkpoint:
        checkpoints = list(sorted(glob.glob(os.path.join(args.output_dir, "*.ckpt"), recursive=True)))
    else:
        checkpoints = [args.checkpoint]

    if checkpoints:
        model.hparams.test_checkpoint = checkpoints[-1]
        trainer.resume_from_checkpoint = checkpoints[-1]

        if args.do_predict and not args.do_train:

            checkpoint = checkpoints[-1]
            print(checkpoint)
            #trainer.test(ckpt_path=checkpoints[-1])
            trainer.test(model, ckpt_path=checkpoint)
            return model


    trainer.logger.log_hyperparams(model.hparams)

    # test() without a model tests using the best checkpoint automatically
    trainer.test()
    return model


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser = pl.Trainer.add_argparse_args(parser)
    parser = SummarizationModule.add_model_specific_args(parser, os.getcwd())

    args = parser.parse_args()

    main(args)
