import os
import logging
import pickle
import json
import numpy as np
import pandas as pd
import torch
import gzip
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from modelscope import AutoTokenizer, AutoModel
from operator import itemgetter
from torch.nn.utils.rnn import pad_sequence

__all__ = ['MMDataLoader']

logger = logging.getLogger('MSA')

class MMDataset(Dataset):
    def __init__(self, args, mode='train'):
        self.mode = mode
        self.args = args
        DATA_MAP = {
            'mosi': self.__init_mosi,
            'mosei': self.__init_mosei,
            'sims': self.__init_sims,
            'simsv2': self.__init_simsv2,
            'meld': self.__init_meld,
            'iemocap': self.__init_iemocap,
            'cherma': self.__init_cherma,

        }
        DATA_MAP[args.datasetName]()



    def __init_meld(self):
        data_path = os.path.join(self.args.dataPath, self.args.datasetName + '_' + self.mode + '.pkl')
        label_index_mapping = self.args.label_index_mapping
        with open(data_path, 'rb') as f:
            data = pickle.load(f)
            self.vision = np.array(list(map(lambda item: item['features']['video'], data))).astype(np.float32)
            self.audio = np.array(list(map(lambda item: item['features']['audio'], data))).astype(np.float32)
            self.rawText = np.array(list(map(lambda item: item['features']['text'], data)))

            # self.labels = {
            #     'M': list(map(lambda item: item['label'], data))
            # }
            self.labels = {
                'M': list(map(lambda item: label_index_mapping.get(item['label'],-1), data))
            }
            if self.args.use_PLM:
                self.text = self.PLM_tokenizer(self.rawText)

        # label_mapping

        # self.labels['M']  = [label_index_mapping.get(label, -1) for label in self.labels['M']]

        if not self.args.need_data_aligned:
            self.audio_lengths = np.array(list(map(lambda item: item['features']['audio_len'], data)))
            self.vision_lengths = np.array(list(map(lambda item: item['features']['video_len'], data)))

    def __init_iemocap(self):
        return self.__init_meld()

    def __init_cherma(self):
        return self.__init_meld()

    def __init_mosi(self):
        with open(self.args.dataPath, 'rb') as f:
            data = pickle.load(f)
            if self.args.use_PLM:
                self.text = data[self.mode]['raw_text']
                self.text = self.PLM_tokenizer(self.text)

        self.vision = data[self.mode]['vision'].astype(np.float32)
        self.audio = data[self.mode]['audio'].astype(np.float32)
        self.rawText = data[self.mode]['raw_text']
        self.ids = data[self.mode]['id']

        self.labels = {
            'M': data[self.mode][self.args.train_mode+'_labels'].astype(np.float32)
        }

        if self.args.need_label_prefix:
            labels = self.labels['M']
            label_prefix = []
            for i in range(len(labels)):
                if labels[i] < 0:
                    label_prefix.append(f'negative,{labels[i].item():.{1}f}')
                elif labels[i] > 0:
                    label_prefix.append(f'positive,{labels[i].item():.{1}f}')
                else:
                    label_prefix.append(f'neutral,{labels[i].item():.{1}f}')
            self.labels_prefix = label_prefix

        if self.args.datasetName == 'sims':
            for m in "TAV":
                self.labels[m] = data[self.mode][self.args.train_mode+'_labels_'+m]

        logger.info(f"{self.mode} samples: {self.labels['M'].shape}")

        if not self.args.need_data_aligned:
            self.audio_lengths = data[self.mode]['audio_lengths']
            self.vision_lengths = data[self.mode]['vision_lengths']
            self.text_lengths = self.args.seq_lens[0]
        self.audio[self.audio == -np.inf] = 0
        self.vision[self.vision != self.vision] = 0

        if  self.args.need_normalized:
            self.__normalize()
    
    def __init_mosei(self):
        return self.__init_mosi()

    def __init_sims(self):
        return self.__init_mosi()

    def __init_simsv2(self):
        return self.__init_mosi()

    def __truncated(self):
        # NOTE: Here for dataset we manually cut the input into specific length.
        def Truncated(modal_features, length):
            if length == modal_features.shape[1]:
                return modal_features
            truncated_feature = []
            padding = np.array([0 for i in range(modal_features.shape[2])])
            for instance in modal_features:
                for index in range(modal_features.shape[1]):
                    if((instance[index] == padding).all()):
                        if(index + length >= modal_features.shape[1]):
                            truncated_feature.append(instance[index:index+20])
                            break
                    else:                        
                        truncated_feature.append(instance[index:index+20])
                        break
            truncated_feature = np.array(truncated_feature)
            return truncated_feature
                       
        text_length, audio_length, video_length = self.args.seq_lens
        self.vision = Truncated(self.vision, video_length)
        self.text = Truncated(self.text, text_length)
        self.audio = Truncated(self.audio, audio_length)

    def __normalize(self):
        # (num_examples,max_len,feature_dim) -> (max_len, num_examples, feature_dim)
        self.vision = np.transpose(self.vision, (1, 0, 2))
        self.audio = np.transpose(self.audio, (1, 0, 2))
        # for visual and audio modality, we average across time
        # here the original data has shape (max_len, num_examples, feature_dim)
        # after averaging they become (1, num_examples, feature_dim)
        self.vision = np.mean(self.vision, axis=0, keepdims=True)
        self.audio = np.mean(self.audio, axis=0, keepdims=True)

        # remove possible NaN values
        self.vision[self.vision != self.vision] = 0
        self.audio[self.audio != self.audio] = 0

        self.vision = np.transpose(self.vision, (1, 0, 2))
        self.audio = np.transpose(self.audio, (1, 0, 2))

    def __len__(self):
        return len(self.labels['M'])

        # 这里text.shape是三维矩阵[sample_num,tokenizer_output,length]
        # tokenizer_output的3个维度分别是token_ids,mask(识别句子中padding的位置),segment_ids
    def get_seq_len(self):
        return (self.text.shape[2], self.audio.shape[1], self.vision.shape[1])

    def get_feature_dim(self):
        return self.text.shape[2], self.audio.shape[2], self.vision.shape[2]

    def PLM_tokenizer (self, rawtexts):
        self.tokenizer = AutoTokenizer.from_pretrained(self.args.pretrain_LM, trust_remote_code=True)
        token_list = []
        for text in rawtexts:
            text_tokenizer = self.tokenizer(text,
                                 padding='max_length',  # 如果样本长度不满足最大长度则填充
                                 truncation=True,  # 截断至最大长度
                                 max_length=self.args.seq_lens[0],
                                 return_tensors = 'pt',
                                 add_special_tokens=False
                                )

            token_ids = text_tokenizer['input_ids'].squeeze(0)  # tensor of token ids  torch.Size([max_len])
            attn_masks = text_tokenizer['attention_mask'].squeeze(0)  # binary tensor with "0" for padded values and "1" for the other values  torch.Size([max_len])
            token_type_ids = [0] * len(token_ids)               #不区分上下句

            #调整维度
            input_ids = np.expand_dims(token_ids, 1)
            input_mask = np.expand_dims(attn_masks, 1)
            segment_ids = np.expand_dims(token_type_ids, 1)

            text_pretrain = np.concatenate([input_ids, input_mask, segment_ids], axis=1).T
            token_list.append(text_pretrain)

        # x_dimensions = [array.shape[1] for array in token_list]
        # # 计算 x 维度的平均值
        # average_x = np.mean(x_dimensions)
        # median_x = np.median(x_dimensions)
        token_list = np.array(token_list)
        return token_list


    def __getitem__(self, index):
        if self.args.train_mode == 'regression':
            sample = {
                'raw_text': self.rawText[index],
                'text': torch.Tensor(self.text[index]),
                'audio': torch.Tensor(self.audio[index]),
                'vision': torch.Tensor(self.vision[index]),
                'index': index,
                'id': self.ids[index],
                'labels': {k: torch.Tensor(v[index].reshape(-1)) for k, v in self.labels.items()},
                'labels_prefix': self.labels_prefix[index]
            }
        else:
            sample = {
                'raw_text': self.rawText[index],
                'text': torch.Tensor(self.text[index]),
                'audio': torch.Tensor(self.audio[index]),
                'vision': torch.Tensor(self.vision[index]),
                'index': index,
                'labels': {k: v[index] for k, v in self.labels.items()}
                # 'labels': {torch.Tensor(self.labels)},
            }

        if not self.args.need_data_aligned:
            sample['audio_lengths'] = self.audio_lengths[index]
            sample['vision_lengths'] = self.vision_lengths[index]
            sample['text_lengths'] = self.args.seq_lens[0]

        return sample



def MMDataLoader(args):

    datasets = {
        'train': MMDataset(args, mode='train'),
        'valid': MMDataset(args, mode='valid'),
        'test': MMDataset(args, mode='test')
    }

    if 'seq_lens' in args:
        args.seq_lens = datasets['train'].get_seq_len() 

    dataLoader = {
        ds: DataLoader(datasets[ds],
                       batch_size=args.batch_size,
                       num_workers=args.num_workers,
                       shuffle=True)
        for ds in datasets.keys()
    }
    
    return dataLoader