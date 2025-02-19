# coding=utf-8

'''
BERT finetuning runner.
支持训练过程中观测模型效果 
created by syzong
2020/4
'''
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import pandas as pd
import csv
import json
import random
import time
import os

import sys

import tokenization
import tensorflow as tf
import pickle
import numpy as np
from metrics import get_multi_metrics
from sklearn.model_selection import StratifiedKFold
from sklearn.model_selection import KFold
# pip install iterative-stratification
from iterstrat.ml_stratifiers import MultilabelStratifiedKFold
from stochastic_weight_averaging import StochasticWeightAveraging
#from eda import eda

# cpu模式下改为 -1
os.environ["CUDA_VISIBLE_DEVICES"] = "1"

flags = tf.flags
FLAGS = flags.FLAGS

## Required parameters
flags.DEFINE_string("data_dir", None, "The input data dir. Should contain the .tsv files (or other data files) for the task.")
flags.DEFINE_string("bert_config_file", None,
                    "The config json file corresponding to the pre-trained BERT model. This specifies the model architecture.")
flags.DEFINE_string("vocab_file", None, "The vocabulary file that the BERT model was trained on.")
flags.DEFINE_string("output_dir", None, "output directory.")
flags.DEFINE_string("model_save", None, "The output directory where the model checkpoints will be written.")

## Other parameters

flags.DEFINE_string("init_checkpoint", None, "Initial checkpoint (usually from a pre-trained BERT model).")
flags.DEFINE_bool("do_lower_case", True, "Whether to lower case the input text. Should be True for uncased models and False for cased models.")
flags.DEFINE_integer("max_seq_length", 128,
                     "The maximum total input sequence length after WordPiece tokenization. Sequences longer than this will be truncated, and sequences shorter than this will be padded.")
flags.DEFINE_bool("do_train", False, "Whether to run training.")
flags.DEFINE_bool("do_load_train", False, "Whether to run incremental training.")
flags.DEFINE_bool("do_predict", False, "Whether to run the model in inference mode on the test set.")
flags.DEFINE_bool("train_arm", False, "Whether arm training.")
flags.DEFINE_integer("train_batch_size", 32, "Total batch size for training.")
flags.DEFINE_integer("eval_batch_size", 16, "Total batch size for eval.")
flags.DEFINE_integer("predict_batch_size", 10, "Total batch size for predict.")
flags.DEFINE_float("learning_rate", 5e-5, "The initial learning rate for Adam.")
flags.DEFINE_integer("num_train_epochs", 30, "Total number of training epochs to perform.")
flags.DEFINE_float("warmup_proportion", 0.1, "Proportion of training to perform linear learning rate warmup for. E.g., 0.1 = 10% of training.")
flags.DEFINE_integer("save_checkpoints_steps", 150, "How often to save the model checkpoint.")
flags.DEFINE_integer("save_checkpoints_epoch", 5, "over the epoch start to save the model checkpoint.")
flags.DEFINE_integer("iterations_per_loop", 1000, "How many steps to make in each estimator call.")

flags.DEFINE_integer("num_labels", 6, "How many steps to make in each estimator call.")

flags.DEFINE_integer("random_seed", 72, "random_seed.")

flags.DEFINE_integer("k_fold", 5, "How many steps to make in each estimator call.")

## adversarial parameters
flags.DEFINE_float("grad_epsilon", 0.5, "FGM adversarial.")

flags.DEFINE_string("model_type", "nezha", "Whether use bert or nezha.")


if FLAGS.model_type == "nezha":
    print("use nezha ...")
    import modeling_nezha as modeling
    import optimization_nezha as optimization
else:
    print("use bert ...")
    import modeling
    import optimization


from sklearn.metrics import log_loss
from sklearn.metrics import roc_auc_score

def convert_float_to_int(intput):
    output = []
    for tmp in intput:
        out_tmp = []
        for tt in tmp:
            tt_new = int(tt + 0.5)
            if tt_new < 0:
                tt_new = 0
            if tt_new > 3:
                tt_new = 3

            out_tmp.append(tt_new)

        output.append(out_tmp)
    return output

def convert_float_to_int_mse(intput,is_eval=False):
    output = []
    for tmp in intput:
        out_tmp = []
        for tt in tmp:
            tt_new = tt
            if tt_new < 0:
                tt_new = 0

            if tt_new > 3:
                tt_new = 3


            out_tmp.append(tt_new)

        output.append(out_tmp)
    return output

def acc_and_f1(pred_y, true_y, eval=True):
    pred_y = convert_float_to_int(pred_y)
    pred_y = np.array(pred_y)
    true_y = np.array(true_y)
    acc_all = []
    f1_all = []

    for i in range(6):
        pred_tmp = []
        true_tmp = []
        for j in range(len(pred_y)):
            pred_tmp.append(pred_y[j][i])
            true_tmp.append(true_y[j][i])

        acc_, recall_, prec, f_beta = get_multi_metrics(pred_y=pred_tmp, true_y=true_tmp, labels=[0, 1, 2, 3])
        acc_all.append(acc_)
        f1_all.append(f_beta)

    if eval:
        print(acc_all, np.mean(acc_all))
        print(f1_all, np.mean(f1_all))

    return np.mean(acc_all), np.mean(f1_all)

def convert_2_pred(prop_list):
    pred_all = []
    for pp in prop_list:
        pos = 0
        pred_tmp = []
        for i in range(6):
            tmp = pp[pos:pos+4]
            lab = tmp.index(max(tmp))
            pred_tmp.append(lab)
            pos += 4

        pred_all.append(pred_tmp)

    return pred_all

def rmse(pred_y, true_y, is_eval=False):
    pred_y = np.array(pred_y)
    true_y = np.array(true_y)

    all_score = 0.0

    for i in range(len(pred_y)):
        true_tmp = true_y[i]
        pred_tmp = pred_y[i]

        # if is_eval:
        #     pred_tmp_new = [pp / 2 for pp in pred_tmp]
        #     pred_tmp = pred_tmp_new

        for j in range(len(true_tmp)):
            all_score = all_score + (pred_tmp[j] - true_tmp[j])**2

    all_score = all_score / (len(pred_y) * 6)
    all_score = np.sqrt(all_score)
    all_score = 1 / (1 + all_score)

    return all_score

def rmse_int(pred_y, true_y, is_eval=False):
    pred_y = convert_float_to_int_mse(pred_y, is_eval)
    pred_y = np.array(pred_y)
    true_y = np.array(true_y)

    all_score = 0.0

    for i in range(len(pred_y)):
        true_tmp = true_y[i]
        pred_tmp = pred_y[i]

        # if is_eval:
        #     pred_tmp_new = [pp / 2 for pp in pred_tmp]
        #     pred_tmp = pred_tmp_new

        for j in range(len(true_tmp)):
            all_score = all_score + (pred_tmp[j] - true_tmp[j])**2

    all_score = all_score / (len(pred_y) * 6)
    all_score = np.sqrt(all_score)
    all_score = 1 / (1 + all_score)

    return all_score

def accuracy(pred_y, true_y):
    pred_y = convert_float_to_int(pred_y)
    pred_y = np.array(pred_y)
    corr = 0
    all = 0

    for i in range(len(pred_y)):
        true_tmp = true_y[i]
        pred_tmp = pred_y[i]

        correct = True
        for j in range(len(true_tmp)):
            if pred_tmp[j] != true_tmp[j]:
                correct = False
                break

        if correct:
            corr += 1

        all += 1

    acc = corr / all
    return acc


def wash_data(w_str):
    if len(w_str) < 1:
        return w_str

    w_str = w_str.strip(" ")
    w_str = w_str.strip("\n")
    w_str = w_str.strip("\r")

    w_str = w_str.replace('—', "-")
    w_str = w_str.replace('“', "'")

    w_str = w_str.replace('”', "'")
    w_str = w_str.replace('"', "'")

    w_str = w_str.replace('[', "")
    w_str = w_str.replace(']', "")
    return w_str

train_0_lab_dict = {}

def truncate_seq_pair_new(tokens_a, tokens_b, max_num_tokens):
    """Truncates a pair of sequences to a maximum sequence length."""
    while True:
        total_length = len(tokens_a) + len(tokens_b)
        if total_length <= max_num_tokens:
            break

        trunc_tokens = tokens_a if len(tokens_a) > len(tokens_b) else tokens_b
        assert len(trunc_tokens) >= 1
        if trunc_tokens == tokens_a:
            del trunc_tokens[0]
        else:
            trunc_tokens.pop()

def truncate_seq_pair(tokens_a, tokens_b, max_num_tokens):
    """Truncates a pair of sequences to a maximum sequence length."""
    while True:
        total_length = len(tokens_a) + len(tokens_b)
        if total_length <= max_num_tokens:
            break

        trunc_tokens = tokens_a if len(tokens_a) > len(tokens_b) else tokens_b
        assert len(trunc_tokens) >= 1
        #del trunc_tokens[0]
        trunc_tokens.pop()

def get_max_count(data_list):
    count = -1
    max_data = ""
    for dd in data_list:
        count_tmp = data_list.count(dd)
        if count_tmp >= count:
            count = count_tmp
            max_data = dd

    return max_data


def get_data_ids_all(file_path, is_test=False):

    data_ids_all = []


    # train : id	content	character	emotions
    # test : id	content	character

    fr = open(file_path, "r")
    all_data = fr.readlines()[1:]

    for data_tmp in all_data:
        data_tmp = data_tmp.strip("\r")
        data_tmp = data_tmp.strip("\n")
        data_tmp_list = data_tmp.split("\t")


        id = data_tmp_list[0]
        id_list = id.split("_")
        first_id = id_list[0]
        sec_id = id_list[1]
        third_id = id_list[3]

        # 统计全是0 的剧场
        # if len(data_tmp_list[3]) > 0:
        #     label_id_str_list = data_tmp_list[3].split(",")
        #
        #     if first_id not in train_0_lab_dict:
        #         train_0_lab_dict[first_id] = {}
        #     if sec_id not in train_0_lab_dict[first_id]:
        #         train_0_lab_dict[first_id][sec_id] = 0
        #
        #     for lll in label_id_str_list:
        #         train_0_lab_dict[first_id][sec_id] += int(lll)
        data_id_tmp = first_id

        # data_id_tmp = first_id + "_" + sec_id
        # #data_id_tmp = first_id
        #
        # if is_test:
        #     data_id_tmp = first_id + "_" + third_id

        if data_id_tmp not in data_ids_all:
            data_ids_all.append(data_id_tmp)

    return data_ids_all

emotion_list = ["爱,","乐,","惊,","怒,","恐,","哀,"]
level_list = ["没有", "有点", "很", "非常"]

import re
def get_all_people(concent):
    all_peop = []
    all_peop = re.findall("[a-z][0-9]", concent)

    return all_peop

test_label_all = {}
all_history_dict = {}
all_history_dict_org = {}
all_sec_dict = {}

def get_all_dict_data(is_training=True):
    if is_training:
        fr_his = open("./data/train_dataset_v2.tsv", "r")
    else:
        fr_his = open("./data/test_dataset.tsv", "r")


    data_resd_all = fr_his.readlines()[1:]

    for his in data_resd_all:
        his = his.strip("\r")
        his = his.strip("\n")
        his_list = his.split("\t")
        id = his_list[0]
        concent = his_list[1]
        character = his_list[2]

        #id_test = id.replace("_","[_]")

        id_list = id.split("_")
        first_id = id_list[0]
        sec_id = id_list[1]
        thir_id = int(id_list[3])


        if first_id not in all_history_dict:
            all_history_dict[first_id] = {}

        if character == "":
            character = "空白"

        all_history_dict[first_id][thir_id] = concent + "\t" + character + "\t" + "情感未知。"

        if first_id not in all_history_dict_org:
            all_history_dict_org[first_id] = {}

        if len(his_list[3]) > 0:
            label_id_str_list = his_list[3].split(",")
            lab_int_list = []
            for lab in label_id_str_list:
                lab_int_list.append(int(lab))

            str_org = get_emo_str(lab_int_list)
            if str_org == "":
                all_history_dict_org[first_id][thir_id] = concent + "\t"
            else:
                all_history_dict_org[first_id][thir_id] = concent + "\t" + character + "\t" + str_org[:-1] + "。"



        # if is_training:
        #     if len(his_list[3]) > 0 and character != "":
        #         label_id_str_list = his_list[3].split(",")
        #         lab_int_list = []
        #         for lab in label_id_str_list:
        #             lab_int_list.append(int(lab))
        #         if np.sum(lab_int_list) > 0:
        #             emo_str = ""
        #             for idxx, ll in enumerate(lab_int_list):
        #                 if ll > 0:
        #                     emo_str = emo_str + level_list[ll] + emotion_list[idxx]
        #
        #             all_history_dict[first_id][thir_id] = concent + "\t" + character + ":" + emo_str[:-1] + "。"
        #         else:
        #             all_history_dict[first_id][thir_id] = concent + "\t" + character + ":无情感。"
        # else:
        #     id_tmp = first_id + "_" + str(thir_id)
        #     if id_tmp in test_label_all:
        #         emo_str = ""
        #         for idxx, ll in enumerate(test_label_all[id_tmp]):
        #             if ll > 0:
        #                 emo_str = emo_str + level_list[ll] + emotion_list[idxx]
        #         all_history_dict[first_id][thir_id] = concent + "\t" + character + ":" + emo_str[:-1] + "。"
        #     else:
        #         all_history_dict[first_id][thir_id] = concent + "\t" + character + ":无情感。"

        # if first_id not in all_sec_dict:
        #     all_sec_dict[first_id] = {}
        # if sec_id not in all_sec_dict[first_id]:
        #     all_sec_dict[first_id][sec_id] = {}
        #
        # if is_training and len(his_list[3]) == 0:
        #     all_sec_dict[first_id][sec_id][thir_id] = concent + "\t" + character + "\t0"
        # else:
        #     all_sec_dict[first_id][sec_id][thir_id] = concent + "\t" + character + "\t1"
        #
        # if is_training == False:
        #     all_sec_dict[first_id][sec_id][thir_id] = concent + "\t" + character + "\t1"



def get_data_ids_count(data_ids, is_training=True):
    contt = 0
    if is_training:
        fr_his = open("./data/train_dataset_v2.tsv", "r")
    else:
        fr_his = open("./data/test_dataset.tsv", "r")

    data_resd_all = fr_his.readlines()[1:]
    for i, data_tmp in enumerate(data_resd_all):
        data_tmp = data_tmp.strip("\r")
        data_tmp = data_tmp.strip("\n")
        data_tmp_list = data_tmp.split("\t")

        id = data_tmp_list[0]
        content = data_tmp_list[1]
        character = data_tmp_list[2]

        id_list = id.split("_")
        first_id = id_list[0]
        sec_id = id_list[1]
        thir_id = int(id_list[3])

        #cur_id = first_id + "_" + sec_id

        cur_id = first_id
        if cur_id not in data_ids:
            continue

        if len(data_tmp_list[3]) == 0:
            continue

        contt += 1

    return contt



def get_data_ids_new(batch_data_org,is_training=True, is_train_flag=False):
    tokenizer = tokenization.FullTokenizer(vocab_file=FLAGS.vocab_file, do_lower_case=FLAGS.do_lower_case)
    id_keys_batch = []
    inputs_ids = []
    input_masks = []
    segment_ids = []
    lab_ids_all = []


    #train : id	content	character	emotions
    #test : id	content	character


    for i, data_tmp in enumerate(batch_data_org):
        data_tmp = data_tmp.strip("\r")
        data_tmp = data_tmp.strip("\n")
        data_tmp_list = data_tmp.split("\t")

        id = data_tmp_list[0]
        content = data_tmp_list[1]
        character = data_tmp_list[2]

        id_list = id.split("_")
        first_id = id_list[0]
        sec_id = id_list[1]
        thir_id = int(id_list[3])

        cur_id = first_id + "_" + sec_id

        key_id = first_id + "\t" + str(thir_id)
        id_keys_batch.append(key_id)

        if is_training:
            label_id = []
            label_id_str_list = data_tmp_list[3].split(",")
            for lab in label_id_str_list:
                    label_id.append(int(lab))
        else:
            label_id = [0, 0, 0, 0, 0, 0]

        lab_ids_all.append(label_id)


        #纠正people
        # his_sec_dict = all_sec_dict[first_id][sec_id]
        # peop_list_bak = []
        # thir_id_min_peop = thir_id - 1
        # min_key_peop= min(his_sec_dict.keys())
        #
        # while True:
        #     if thir_id_min_peop < min_key_peop:
        #         break
        #     if thir_id_min_peop in his_sec_dict:
        #         cur_history = his_sec_dict[thir_id_min_peop]
        #         cur_history_list = cur_history.split("\t")
        #         if cur_history_list[0] == content and cur_history_list[1] not in peop_list_bak and cur_history_list[2] == "1":
        #             peop_list_bak.append(cur_history_list[1])
        #
        #         if cur_history_list[0] != content:
        #             break
        #
        #     thir_id_min_peop -= 1
        #
        # cur_people_list = list(set(get_all_people(content)))
        #
        #
        # if len(peop_list_bak) > 0:
        #     for pepp in peop_list_bak:
        #         if pepp in cur_people_list:
        #             cur_people_list.remove(pepp)
        #
        # if len(cur_people_list) > 0 and character not in cur_people_list:
        #     print(id,content,character,cur_people_list[0])
        #     character = cur_people_list[0]
        #     contt += 1


        #his_tmp_dict = all_history_dict[first_id][sec_id]
        if is_train_flag:
            his_tmp_dict = all_history_dict_org[first_id]
        else:
            his_tmp_dict = all_history_dict[first_id]
        history_str_before = []
        history_tmp = ""
        count = 0
        thir_id_min = thir_id - 1
        min_key = min(his_tmp_dict.keys())

        while count < 5:
            if thir_id_min < min_key:
                break
            if thir_id_min in his_tmp_dict:
                cur_history_l = his_tmp_dict[thir_id_min].split("\t")
                cur_history = cur_history_l[0]

                # if cur_history == history_tmp and len(cur_history_l[1]) > 0:
                #     history_str_before = history_str_before + ";" + cur_history_l[1]

                if cur_history == content:
                    thir_id_min -= 1
                    continue
                if cur_history == history_tmp:
                    if cur_history_l[1] != "空白" and cur_history_l[1] != "":
                        history_str_before[-1] = history_str_before[-1] + cur_history_l[1]  + ":" + cur_history_l[2]
                    thir_id_min -= 1
                    continue

                count += 1
                if cur_history_l[1] != "" and cur_history_l[1] != "空白":
                    history_str_before_tmp = cur_history + cur_history_l[1]  + ":" + cur_history_l[2]
                else:
                    history_str_before_tmp = cur_history
                history_str_before.append(history_str_before_tmp)
                history_tmp = cur_history

            thir_id_min -= 1



        if character == "":
            character = "空白"

        content = wash_data(content)
        character = wash_data(character)



        #character_replace = "[" + character + "]"
        #content = content.replace(character,character_replace)

        content = "[描述角色是" + character + "]" + content

        # character_new = character
        # #character_new = character + "的" + emo_str + "是什么"

        content_token = []
        character_token = []
        history_token = []
        history_token_before = []
        history_token_after = []

        if len(history_str_before) < 1:
            history_token_before = tokenizer.tokenize(tokenization.convert_to_unicode("无上文。")) + ["[SEP]"]
        else:
            for be_str in history_str_before:
                be_str = wash_data(be_str)
                token_before_tmp =[]
                for con in be_str:
                    con_t = tokenizer.tokenize(tokenization.convert_to_unicode(con))
                    token_before_tmp.extend(con_t)
                history_token_before = token_before_tmp + ["[SEP]"] + history_token_before

        # content_token = tokenizer.tokenize(tokenization.convert_to_unicode(content))
        # history_token_before = tokenizer.tokenize(tokenization.convert_to_unicode(history_str_before))
        # history_token_after = tokenizer.tokenize(tokenization.convert_to_unicode(history_str_after))

        for con in content:
            con_t = tokenizer.tokenize(tokenization.convert_to_unicode(con))
            content_token.extend(con_t)

        # for his in history_str_before:
        #     his_t = tokenizer.tokenize(tokenization.convert_to_unicode(his))
        #     history_token_before.extend(his_t)

        # for his in history_str_after:
        #     his_t = tokenizer.tokenize(tokenization.convert_to_unicode(his))
        #     history_token_after.extend(his_t)

        # if len(content_token) > FLAGS.max_seq_length/2:
        #     truncate_seq_pair_new(history_token_before, history_token_after, FLAGS.max_seq_length/2 - 10)
        # else:
        #     truncate_seq_pair_new(history_token_before, history_token_after, FLAGS.max_seq_length-len(content_token) -10)
        #
        # left_len = FLAGS.max_seq_length - len(history_token_before) - len(history_token_after) - 10
        truncate_seq_pair_new(history_token_before, content_token, FLAGS.max_seq_length-2)

        if history_token_before[-1] != "[SEP]":
            print("history_token_before data error ..")

        # be_o = tokenizer.tokenize(tokenization.convert_to_unicode("上文:"))
        # history_token_before = be_o + history_token_before
        #
        # af_o = tokenizer.tokenize(tokenization.convert_to_unicode("下文:"))
        # history_token_after = af_o + history_token_after

        tokens_all = ["[CLS]"] + content_token + ["[SEP]"] + history_token_before
        segment_id_tmp = [0] * (len(content_token) + 2) + [1] * (len(history_token_before))

        inputs_id_tmp = tokenizer.convert_tokens_to_ids(tokens_all)
        input_mask_tmp = [1] * len(inputs_id_tmp)

        if len(inputs_id_tmp) < FLAGS.max_seq_length:
            inputs_id_tmp.extend([0] * (FLAGS.max_seq_length - len(inputs_id_tmp)))
            input_mask_tmp.extend([0] * (FLAGS.max_seq_length - len(input_mask_tmp)))
            segment_id_tmp.extend([0] * (FLAGS.max_seq_length - len(segment_id_tmp)))

        inputs_ids.append(inputs_id_tmp)
        input_masks.append(input_mask_tmp)
        segment_ids.append(segment_id_tmp)


    return id_keys_batch, inputs_ids, input_masks, segment_ids, lab_ids_all

def next_batch(batch_size, input_ids, input_masks, segment_ids, label_ids):
    totle_len = len(input_ids)
    num_batches = len(input_ids) // batch_size
    if_remain = False
    if totle_len > num_batches * batch_size:
        if_remain = True
        num_batches = num_batches + 1
    for i in range(num_batches):
        if if_remain and i == num_batches - 1:
            start = i * batch_size
            end = totle_len
        else:
            start = i * batch_size
            end = start + batch_size
        batch_input_ids = input_ids[start: end]
        batch_input_masks = input_masks[start: end]
        batch_segment_ids = segment_ids[start: end]
        batch_label_ids = label_ids[start: end]

        yield dict(input_ids=batch_input_ids, input_masks=batch_input_masks, segment_ids=batch_segment_ids, label_ids=batch_label_ids)


def create_model(bert_config, is_training, input_ids, input_mask, segment_ids, labels, num_labels, embed_grad_holder,
                 drop_holder, hidden_dropout_prob_holder, attention_probs_dropout_prob_holder):
    """Creates a classification model."""
    model = modeling.BertModel(
        config=bert_config,
        input_ids=input_ids,
        input_mask=input_mask,
        token_type_ids=segment_ids,
        level_type_ids=None,
        use_one_hot_embeddings=False,
        embed_grad_adv=embed_grad_holder,
        hidden_dropout_prob=hidden_dropout_prob_holder,
        attention_probs_dropout_prob=attention_probs_dropout_prob_holder)

    # layer_logits = []
    # for i, layer in enumerate(model.all_encoder_layers):
    #     layer_logits.append(
    #         tf.layers.dense(
    #             layer, 1,
    #             kernel_initializer=tf.truncated_normal_initializer(stddev=0.02), name="layer_logit%d" % i
    #         )
    #     )

    # layer_logits = tf.concat(layer_logits, axis=2)  # 第三维度拼接
    # layer_dist = tf.nn.softmax(layer_logits)
    # seq_out = tf.concat([tf.expand_dims(x, axis=2) for x in model.all_encoder_layers], axis=2)
    # pooled_output = tf.matmul(tf.expand_dims(layer_dist, axis=2), seq_out)
    # pooled_output = tf.squeeze(pooled_output, axis=2)
    # maxpool_layer = tf.keras.layers.GlobalMaxPooling1D()(pooled_output)
    # output_layer = tf.layers.dense(
    #     maxpool_layer,
    #     bert_config.hidden_size,
    #     activation=tf.nn.relu)

    # In the demo, we are doing a simple classification task on the entire
    # segment.
    #
    # If you want to use the token-level output, use model.get_sequence_output()
    # instead.
    # new
    seq_output = model.get_sequence_output()
    a = tf.keras.layers.GlobalAveragePooling1D()(seq_output)
    b = tf.keras.layers.GlobalMaxPooling1D()(seq_output)
    # c = seq_output[:, -1, :]
    # d = seq_output[:, 0, :]
    # seq_out_final = tf.concat([a, b, c, d], axis=1)
    # seq_out_final = tf.reshape(seq_out_final, [-1, 4 * bert_config.hidden_size])
    # output_layer = tf.layers.dense(
    #     seq_out_final,
    #     bert_config.hidden_size,
    #     activation=tf.tanh,
    #     # activation=tf.nn.relu,  # TODO
    #     kernel_initializer=tf.truncated_normal_initializer(stddev=0.02))

    # output_layer = tf.layers.dense(output_layer, FLAGS.num_labels)
    # old

    pooled_layer = model.get_pooled_output()
    output_layer = tf.concat([pooled_layer, a], axis=1)
    output_layer = tf.reshape(output_layer, [-1, 2 * bert_config.hidden_size])

    #output_layer = tf.layers.dense(output_layer, 512, activation="relu")

    #output_layer = model.get_pooled_output()


    label_smoothing = 0.0001
    num_labels_2 = 4

    # with tf.variable_scope("loss"):
    #     # labels = labels * (1 - label_smoothing) + label_smoothing / num_labels
    #     if is_training:
    #         output_layer = tf.nn.dropout(output_layer, keep_prob=drop_holder)
    #
    #     loss_all = []
    #     prop_all = []
    #
    #     logits = tf.layers.dense(output_layer, num_labels)
    #     loss_final = tf.losses.mean_squared_error(labels,logits)
    #
    #
    #
    #     prop_all_final = logits
    #
    #
    #     return (loss_final, None, prop_all_final, prop_all_final, None)

    with tf.variable_scope("loss"):
        # labels = labels * (1 - label_smoothing) + label_smoothing / num_labels
        if is_training:
            output_layer = tf.nn.dropout(output_layer, keep_prob=drop_holder)

        loss_all = []
        prop_all = []

        for layer_idx in range(FLAGS.num_labels):
            with tf.variable_scope("dense_%d" % layer_idx):
                logits = tf.layers.dense(output_layer, 1)

                lab_tmp = labels[:, layer_idx:(layer_idx + 1)]

                loss_tmp = tf.losses.mean_squared_error(lab_tmp, logits)
                loss_all.append(loss_tmp)
                prop_all.append(logits)



        loss_final = tf.reduce_sum(loss_all)
        prop_all_final = tf.concat(prop_all, axis=1)

        return (loss_final, None, prop_all_final, prop_all_final, None)

def get_all_data(file_name, id_in):
    fr_his = open(file_name, "r")
    all_data_dict_org = {}
    data_resd_all = fr_his.readlines()[1:]

    for i, data_tmp in enumerate(data_resd_all):
        data_tmp = data_tmp.strip("\r")
        data_tmp = data_tmp.strip("\n")
        data_tmp_list = data_tmp.split("\t")

        id = data_tmp_list[0]
        content = data_tmp_list[1]
        character = data_tmp_list[2]

        id_list = id.split("_")
        first_id = id_list[0]
        sec_id = id_list[1]
        thir_id = int(id_list[3])

        #cur_id = first_id + "_" + sec_id

        cur_id = first_id

        if cur_id not in id_in:
            continue

        if len(data_tmp_list[3]) == 0:
            continue

        if cur_id not in all_data_dict_org:
            all_data_dict_org[cur_id] = {}

        all_data_dict_org[cur_id][thir_id] = data_tmp


    #sort
    all_data_dict_org_order = {}

    for k ,v in all_data_dict_org.items():
        v_order = sorted(v.items(),key=lambda x:x[0],reverse=False)
        all_data_dict_org_order[k] = {}
        for v_tmp in v_order:
            all_data_dict_org_order[k][v_tmp[0]] = v_tmp[1]

    #all_data_dict_org_order = sorted(all_data_dict_org.items(),key=lambda x:x[1],reverse=False)

    return all_data_dict_org_order

import copy

def get_batch_data(data_all_dict,batch_size):
    batch_data = []

    if len(data_all_dict) == 0:
        return batch_data

    count = 0
    data_all_dict_new = copy.deepcopy(data_all_dict)


    for k,v in data_all_dict.items():
        for k2,v2 in v.items():
            batch_data.append(v2)
            data_all_dict_new[k].pop(k2)
            count += 1
            break

        if count >= batch_size:
            break


    return batch_data, data_all_dict_new

def get_emo_str(pp_list):
    emo_str = ""
    for idxx, ppp in enumerate(pp_list):
        ll = 0
        if ppp < 0.5:
            ll = 0
        elif 0.5 <= ppp < 1.5:
            ll = 1
        elif 1.5 <= ppp < 2.5:
            ll = 2
        else:
            ll = 3

        if ll > 0:
            emo_str = emo_str + level_list[ll] + emotion_list[idxx]

    # if emo_str == "":
    #     emo_str = "无任何情感。"

    return emo_str

def get_emo_str_2(pp_list):
    emo_str = ""
    for idxx, ppp in enumerate(pp_list):
        pp_str = str(round(ppp,2))
        emo_str = emo_str + pp_str + emotion_list[idxx]

    return emo_str


def main(_):
    # tf.logging.set_verbosity(tf.logging.INFO)
    tokenization.validate_case_matches_checkpoint(FLAGS.do_lower_case, FLAGS.init_checkpoint)

    if not FLAGS.do_train and not FLAGS.do_predict:
        raise ValueError("At least one of `do_train`, `do_predict', must be True.")

    bert_config = modeling.BertConfig.from_json_file(FLAGS.bert_config_file)
    seed_nub = FLAGS.random_seed
    print("seed number .. ",seed_nub)

    random.seed(seed_nub)
    tf.set_random_seed(seed_nub)
    np.random.seed(seed_nub)


    if FLAGS.do_train:

        train_data_ids_all = get_data_ids_all("./data/train_dataset_v2.tsv")
        train_data_ids_all = np.array(train_data_ids_all)
        num_labels = FLAGS.num_labels
        kfd_num = 0
        k_fold = FLAGS.k_fold

        #mskf = MultilabelStratifiedKFold(n_splits=k_fold, shuffle=True, random_state=seed_nub)

        #mskf = StratifiedKFold(n_splits=k_fold, shuffle=True, random_state=seed_nub)

        mskf = KFold(n_splits=k_fold, shuffle=True, random_state=seed_nub)

        dev_mloss_all = []

        for train_index, dev_index in mskf.split(train_data_ids_all):
            # # print("TRAIN:", train_index, "DEV:", dev_index)
            # X_train, X_dev = data_in_z[train_index], data_in_z[dev_index]
            # y_train, y_dev = all_lab_ids[train_index], all_lab_ids[dev_index]

            get_all_dict_data(True)

            tf.reset_default_graph()
            output_dir = FLAGS.output_dir + "-fold-" + str(kfd_num)
            tf.gfile.MakeDirs(output_dir)
            FLAGS.model_save = output_dir + "/model"
            kfd_num += 1
            print("Fold  %d ......" % (kfd_num))

            train_ids = train_data_ids_all[train_index]
            dev_ids = train_data_ids_all[dev_index]

            train_ids.sort()
            dev_ids.sort()

            train_data_count = get_data_ids_count(train_ids)


            num_train_steps = int(train_data_count / FLAGS.train_batch_size * FLAGS.num_train_epochs)
            num_warmup_steps = int(num_train_steps * FLAGS.warmup_proportion)
            FLAGS.save_checkpoints_steps = int(num_train_steps / 16)

            print("label numbers: ", num_labels)

            input_ids_holder = tf.placeholder(dtype=tf.int32, shape=[None, FLAGS.max_seq_length], name='input_ids_holder')
            input_masks_holder = tf.placeholder(dtype=tf.int32, shape=[None, FLAGS.max_seq_length], name='input_mask_holder')
            segment_ids_holder = tf.placeholder(dtype=tf.int32, shape=[None, FLAGS.max_seq_length], name='segment_ids_holder')
            label_ids_holder = tf.placeholder(dtype=tf.float32, shape=[None, num_labels], name="label_ids_holder")
            embed_grad_holder = tf.placeholder(dtype=tf.float32, shape=[None, None], name="embed_grad_holder")
            drop_holder = tf.placeholder(dtype=tf.float32, name="drop_holder")
            hidden_dropout_prob_holder = tf.placeholder(dtype=tf.float32, name="hidden_dropout_prob_holder")
            attention_probs_dropout_prob_holder = tf.placeholder(dtype=tf.float32, name="attention_probs_dropout_prob_holder")

            loss, per_example_loss, logits, probabilities, predictions = create_model(bert_config, True, input_ids_holder, input_masks_holder,
                                                                                      segment_ids_holder, label_ids_holder, num_labels,
                                                                                      embed_grad_holder,
                                                                                      drop_holder, hidden_dropout_prob_holder,
                                                                                      attention_probs_dropout_prob_holder)
            train_op = optimization.create_optimizer(loss, FLAGS.learning_rate, num_train_steps, num_warmup_steps)

            # fine_tine_learning_rate = 1e-3
            # decay_rate = 0.95
            # decay_step = 0.5 * num_train_steps
            #
            # global_step = tf.Variable(0, name='step', trainable=False)
            # learning_rate = tf.train.exponential_decay(fine_tine_learning_rate, global_step, decay_step, decay_rate, staircase=True)
            # # 下接结构的学习率
            # normal_optimizer = tf.train.AdamOptimizer(learning_rate)
            #
            # all_variables = tf.trainable_variables()
            # # BERT的参数
            # word2vec_var_list = [x for x in all_variables if 'bert' in x.name]
            #
            # # 下接结构的参数
            # normal_var_list = [x for x in all_variables if 'bert' not in x.name]
            # #print('bert train variable num: {}'.format(len(word2vec_var_list)))
            # #print('normal train variable num: {}'.format(len(normal_var_list)))
            #
            # normal_op = normal_optimizer.minimize(loss, global_step=global_step, var_list=normal_var_list)
            #
            # if word2vec_var_list:  # 对BERT微调
            #     print('word2vec trainable!!')
            #     word2vec_op = optimization.create_optimizer_new(loss, FLAGS.learning_rate, num_train_steps, num_warmup_steps,
            #                                                     variable_list=word2vec_var_list)
            #     # 组装BERT与下接结构参数
            #     train_op = tf.group(normal_op, word2vec_op)
            # else:
            #     train_op = normal_op


            # SWA
            # create an op that combines the SWA formula for all trainable weights
            model_vars = tf.trainable_variables()
            swa = StochasticWeightAveraging()
            swa_op = swa.apply(var_list=model_vars)

            # now you can train you model, and EMA will be used, but not in your built network !
            # accumulated weights are stored in ema.average(var) for a specific 'var'
            # so you will evaluate your model with the classical weights, not with EMA weights
            # trick : create backup variables to store trained weights, and operations to set weights use in the network to weights from EMA

            # Make backup variables
            with tf.variable_scope('BackupVariables'), tf.device('/cpu:0'):
                # force tensorflow to keep theese new variables on the CPU !
                backup_vars = [tf.get_variable(var.op.name, dtype=var.value().dtype, trainable=False,
                                               initializer=var.initialized_value())
                               for var in model_vars]

            # operation to assign SWA weights to model
            swa_to_weights = tf.group(*(tf.assign(var, swa.average(var).read_value()) for var in model_vars))
            # operation to store model into backup variables
            save_weight_backups = tf.group(*(tf.assign(bck, var.read_value()) for var, bck in zip(model_vars, backup_vars)))
            # operation to get back values from backup variables to model
            restore_weight_backups = tf.group(*(tf.assign(var, bck.read_value()) for var, bck in zip(model_vars, backup_vars)))

            # embedding 对抗训练
            tvs = tf.trainable_variables()
            embed_variable = tvs[0]
            # embed_grad = optimizer.compute_gradients(loss, embed_variable)
            embed_grad = tf.gradients(loss, embed_variable)

            accum_vars = tf.Variable(tf.zeros_like(embed_variable.initialized_value()), trainable=False)
            embed_tmpp = accum_vars.assign_add(embed_grad[0])
            grad_delta = FLAGS.grad_epsilon * embed_tmpp / (tf.sqrt(tf.reduce_sum(tf.square(embed_tmpp))) + 1e-8)  # 计算扰动

            # max_to_keep最多保存几个模型
            saver = tf.train.Saver(tf.global_variables(), max_to_keep=2)
            with tf.Session() as sess:
                tvars = tf.trainable_variables()
                (assignment_map, initialized_variable_names) = modeling.get_assignment_map_from_checkpoint(tvars, FLAGS.init_checkpoint)
                tf.train.init_from_checkpoint(FLAGS.init_checkpoint, assignment_map)

                sess.run(tf.variables_initializer(tf.global_variables()))
                max_acc = 0
                current_step = 0
                eval_loss_all = 100
                start = time.time()
                stop = False
                embed_zero = np.zeros_like(embed_variable.eval())

                train_data_all_dict = get_all_data("./data/train_dataset_v2.tsv", train_ids)
                dev_data_all_dict_org = get_all_data("./data/train_dataset_v2.tsv", dev_ids)
                train_in_ids_all = []
                train_in_masks_all = []
                train_seg_ids_all = []
                train_lab_ids_all = []
                while True:
                    batch_data_org, dict_new = get_batch_data(train_data_all_dict, FLAGS.train_batch_size)
                    train_data_all_dict = dict_new

                    if len(batch_data_org) < 1:
                        print("no data 1 ...")
                        break

                    id_keys, train_in_ids, train_in_masks, train_seg_ids, train_lab_ids = get_data_ids_new(batch_data_org, True, True)
                    train_in_ids_all += train_in_ids
                    train_in_masks_all += train_in_masks
                    train_seg_ids_all += train_seg_ids
                    train_lab_ids_all += train_lab_ids

                    if len(train_in_ids) < 1:
                        print("no data 2 ...")
                        break
                print(len(train_in_ids_all))
                print(len(train_in_masks_all))
                print(len(train_seg_ids_all))
                print(len(train_lab_ids_all))


                tra_z_tmp = list(zip(train_in_ids_all, train_in_masks_all, train_seg_ids_all, train_lab_ids_all))
                random.shuffle(tra_z_tmp)
                train_in_ids_all, train_in_masks_all, train_seg_ids_all, train_lab_ids_all = zip(*tra_z_tmp)

                for epoch in range(FLAGS.num_train_epochs):
                    print("----- Epoch {}/{} -----".format(epoch + 1, FLAGS.num_train_epochs))

                    for batch in next_batch(FLAGS.train_batch_size, train_in_ids_all, train_in_masks_all, train_seg_ids_all, train_lab_ids_all):
                        feed_dict = {input_ids_holder: batch["input_ids"], input_masks_holder: batch["input_masks"],
                                     segment_ids_holder: batch["segment_ids"], label_ids_holder: batch["label_ids"]}

                        feed_dict[drop_holder] = 0.8
                        feed_dict[hidden_dropout_prob_holder] = 0.1
                        feed_dict[attention_probs_dropout_prob_holder] = 0.1
                        feed_dict[embed_grad_holder] = embed_zero


                        # 梯度对抗
                        grad_delta_tmp = sess.run([grad_delta], feed_dict=feed_dict)
                        feed_dict[embed_grad_holder] = grad_delta_tmp[0]

                        # 训练模型

                        _, train_loss, train_probabilities = sess.run([train_op, loss, probabilities], feed_dict=feed_dict)

                        # for idx, key in enumerate(id_keys):
                        #     k_l = key.split("\t")
                        #     f_id = k_l[0]
                        #     t_id = int(k_l[1])
                        #     prop = train_probabilities[idx]
                        #
                        #     tmp_d = all_history_dict[f_id][t_id]
                        #     tmp_d_l = tmp_d.split("\t")
                        #
                        #     emo_str = get_emo_str(prop)
                        #     all_history_dict[f_id][t_id] = tmp_d_l[0] + "\t" + tmp_d_l[1] + "\t" + emo_str[:-1] + "。"

                        if current_step % 20 == 0:
                            # train_predictions = convert_2_pred(train_probabilities.tolist())
                            # train_labs = convert_2_pred(batch["label_ids"])

                            train_predictions = train_probabilities
                            train_labs = batch["label_ids"]

                            train_rmse = rmse(train_predictions,train_labs)
                            acc_ = accuracy(train_predictions,train_labs)
                            acc_my, f1_my = acc_and_f1(train_predictions,train_labs,False)
                            print("train: total_step: %d, current_step: %d, loss: %.4f, acc: %.4f, rmse: %.6f, acc_my %.4f, f1_my %.4f" %
                                  (num_train_steps, current_step, train_loss, acc_, train_rmse,acc_my,f1_my))

                        current_step += 1
                        if current_step % FLAGS.save_checkpoints_steps == 0 and current_step > 600:
                            # # at the end of the epoch, you can run the SWA op which apply the formula defined above
                            # sess.run(swa_op)
                            #
                            # # now to evaluate the model with SWA weights :
                            # # save weights
                            # sess.run(save_weight_backups)
                            #
                            # # replace weights by SWA ones
                            # sess.run(swa_to_weights)

                            eval_losses = []
                            eval_predictions_all = []
                            eval_prop_all = []
                            eval_prop_all_auc = []
                            label_ids_all = []

                            dev_data_all_dict = copy.deepcopy(dev_data_all_dict_org)

                            while True:
                                dev_batch_data_org, dev_dict_new = get_batch_data(dev_data_all_dict, FLAGS.train_batch_size)
                                dev_data_all_dict = dev_dict_new
                                if len(dev_batch_data_org) < 1:
                                    break

                                dev_id_keys, dev_in_ids, dev_in_masks, dev_seg_ids, dev_lab_ids = get_data_ids_new(dev_batch_data_org)

                            #for eval_batch in next_batch(FLAGS.train_batch_size, dev_in_ids, dev_in_masks, dev_seg_ids, dev_lab_ids):
                                eval_feed_dict = {input_ids_holder: dev_in_ids, input_masks_holder:dev_in_masks,
                                                  segment_ids_holder: dev_seg_ids, label_ids_holder: dev_lab_ids}

                                eval_feed_dict[drop_holder] = 1.0
                                eval_feed_dict[hidden_dropout_prob_holder] = 0.0
                                eval_feed_dict[attention_probs_dropout_prob_holder] = 0.0
                                eval_feed_dict[embed_grad_holder] = embed_zero
                                eval_loss, eval_probabilities = sess.run([loss, probabilities], feed_dict=eval_feed_dict)

                                for idx, key in enumerate(dev_id_keys):
                                    k_l = key.split("\t")
                                    f_id = k_l[0]
                                    t_id = int(k_l[1])
                                    prop = eval_probabilities[idx]

                                    tmp_d = all_history_dict[f_id][t_id]
                                    tmp_d_l = tmp_d.split("\t")

                                    emo_str = get_emo_str(prop)
                                    if emo_str == "":
                                        all_history_dict[f_id][t_id] = tmp_d_l[0] + "\t" + tmp_d_l[1] + "\t" + emo_str[:-1] + "。"
                                    else:
                                        all_history_dict[f_id][t_id] = tmp_d_l[0] + "\t"

                                eval_losses.append(eval_loss)

                                eval_prop_all.extend(eval_probabilities)
                                label_ids_all.extend(dev_lab_ids)

                            # eval_predictions_all = convert_2_pred(eval_prop_all)
                            # eval_labs = convert_2_pred(label_ids_all)

                            eval_predictions_all = eval_prop_all
                            eval_labs = label_ids_all

                            eval_rmse = rmse(eval_predictions_all,eval_labs, True)
                            eval_rmse_int = rmse_int(eval_predictions_all, eval_labs, True)

                            eval_acc_ = accuracy(eval_predictions_all,eval_labs)
                            eval_acc_my, eval_f1_my = acc_and_f1(eval_predictions_all,eval_labs)

                            print("eval:  loss: %.6f, acc: %.6f, rmse: %.6f, rmse_int: %.6f, acc_my %.4f, f1_my %.4f" %
                                  (np.mean(eval_losses), eval_acc_, eval_rmse,eval_rmse_int,eval_acc_my,eval_f1_my))

                            if eval_rmse >= max_acc:
                                print("********** save new model, step {} , dev acc {}, rmse {}".format(current_step, eval_acc_, eval_rmse))
                                max_acc = eval_rmse
                                saver.save(sess, FLAGS.model_save, global_step=current_step)

                            # sess.run(restore_weight_backups)

                end = time.time()
                print("total train time: ", end - start)
                # break
            dev_mloss_all.append(max_acc)

        print(dev_mloss_all, np.mean(dev_mloss_all))

    if FLAGS.do_predict:
        get_all_dict_data(False)

        num_labels = FLAGS.num_labels

        input_ids_holder = tf.placeholder(dtype=tf.int32, shape=[None, FLAGS.max_seq_length], name='input_ids_holder')
        input_masks_holder = tf.placeholder(dtype=tf.int32, shape=[None, FLAGS.max_seq_length], name='input_mask_holder')
        segment_ids_holder = tf.placeholder(dtype=tf.int32, shape=[None, FLAGS.max_seq_length], name='segment_ids_holder')
        label_ids_holder = tf.placeholder(dtype=tf.float32, shape=[None, num_labels], name="label_ids_holder")
        drop_holder = tf.placeholder(dtype=tf.float32, name="drop_holder")
        hidden_dropout_prob_holder = tf.placeholder(dtype=tf.float32, name="hidden_dropout_prob_holder")
        attention_probs_dropout_prob_holder = tf.placeholder(dtype=tf.float32, name="attention_probs_dropout_prob_holder")


        test_data_ids_all = get_data_ids_all("./data/test_dataset.tsv", True)
        test_data_ids_all_org = get_data_ids_all("./data/test_dataset.tsv", True)
        test_data_ids_all.sort()
        test_final_result_pp = {}

        #test_in_ids, test_in_masks, test_seg_ids, test_lab_ids = get_data_ids(test_data_ids_all, False)


        print("****Test*****\n label numbers: ", num_labels)
        loss, per_example_loss, logits, probabilities, predictions = create_model(bert_config, False, input_ids_holder, input_masks_holder,
                                                                                  segment_ids_holder, label_ids_holder, num_labels, None, drop_holder,
                                                                                  hidden_dropout_prob_holder, attention_probs_dropout_prob_holder)
        saver = tf.train.Saver(tf.global_variables())
        tf_config = tf.ConfigProto()
        tf_config.gpu_options.per_process_gpu_memory_fraction = 0.18  # 分配18%
        with tf.Session(config=tf_config) as sess:
            model_file = tf.train.latest_checkpoint(FLAGS.output_dir)
            print("latest model_file:", model_file)
            model_file_all = tf.train.get_checkpoint_state(FLAGS.output_dir)
            model_file_all = model_file_all.all_model_checkpoint_paths
            for num, m_path in enumerate(model_file_all):
                print(m_path)
                saver.restore(sess, m_path)
                start = time.time()
                test_prob_all = []

                for numm_id, test_data_id in enumerate(test_data_ids_all):
                    if numm_id % 100 == 0:
                        print(numm_id)
                    test_in_ids, test_in_masks, test_seg_ids, test_lab_ids = get_data_ids([test_data_id], False)

                    test_feed_dict = {input_ids_holder: test_in_ids, input_masks_holder: test_in_masks,
                                      segment_ids_holder: test_seg_ids, label_ids_holder: test_lab_ids,
                                      drop_holder: 1.0, hidden_dropout_prob_holder: 0.0, attention_probs_dropout_prob_holder: 0.0}

                    test_loss, test_porp = sess.run([loss, probabilities], feed_dict=test_feed_dict)
                    test_porp_l = test_porp.tolist()
                    test_prob_all.extend(test_porp_l)

                    test_final_result_pp[test_data_id] = test_porp_l[0]

                    test_lab_tmp = []

                    for ppp in test_porp_l[0]:
                        if ppp < 0.5:
                            test_lab_tmp.append(0)
                        elif 0.5 <= ppp < 1.5:
                            test_lab_tmp.append(1)
                        elif 1.5 <= ppp < 2.5:
                            test_lab_tmp.append(2)
                        else:
                            test_lab_tmp.append(3)

                    if np.sum(test_lab_tmp) > 0:
                        test_label_all[test_data_id] = test_lab_tmp



                output_predict_file = os.path.join(FLAGS.output_dir, ("prop_" + str(num) + ".txt"))
                with tf.gfile.GFile(output_predict_file, "w") as writer:
                    pred_len = len(test_data_ids_all_org)
                    print("pred_len : %d\n" % (pred_len))
                    for tes_id in test_data_ids_all_org:
                        prop = test_final_result_pp[tes_id]
                        w_str = ""
                        for pp in prop:
                            w_str = w_str + str(pp) + ","
                        w_str = w_str[:-1] + "\n"
                        writer.write(w_str)


                end = time.time()
                print("total test time: ", end - start)


if __name__ == "__main__":
    # flags.mark_flag_as_required("data_dir")
    # flags.mark_flag_as_required("vocab_file")
    # flags.mark_flag_as_required("bert_config_file")
    # flags.mark_flag_as_required("output_dir")
    tf.app.run()
