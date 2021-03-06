import queue
import threading

import numba
import numpy as np
import torch
import torch.nn as nn
from torch.autograd import Function, Variable

from .utils import log_sum_exp


@numba.jit(nogil=True)
def _gram_ctc_loss(logits, targets, grams, blank_idx=0):
    """
    http://www.cs.toronto.edu/~graves/icml_2006.pdf
    :param logits: numpy array, sequence_len * num_labels
    :param targets: numpy array, target labels
    :param blank: blank index
    :return: loss (float), gradient (same shape as logits)
    """
    targets_len = targets.shape[0]
    prediction_len = logits.shape[0]
    num_labels = logits.shape[1]

    max_gram_length = len(grams.shape)
    if max_gram_length >= 4:
        raise NotImplementedError

    # num_basic_labels = grams.shape[0]

    extended_targets = np.ones(targets_len * (max_gram_length + 1) + 1, dtype=np.int64) * blank_idx
    extended_targets_num_grams = np.ones(targets_len * (max_gram_length + 1) + 1, dtype=np.int64)

    extended_targets_len = 0
    for i in range(targets_len):
        extended_targets_len += 1  # here was blank, it is in just in extended_targets
        extended_targets[extended_targets_len] = targets[i]
        extended_targets_len += 1
        for start in range(i - 1, max(-1, i - max_gram_length), -1):
            gram_indices = tuple(targets[j] for j in range(start, i + 1)) \
                           + tuple(
                0 for _ in range(max_gram_length - (i - start + 1)))  # can't use dict because of numba limitations
            current_ngram = grams[gram_indices]
            if current_ngram > 0:  # if such ngram exists
                extended_targets[extended_targets_len] = current_ngram
                extended_targets_num_grams[extended_targets_len] = i - start + 1
                extended_targets_len += 1
    extended_targets_len += 1  # last blank

    # alpha and beta computation

    # forward - alpha
    log_alpha = np.zeros((extended_targets_len, prediction_len))
    log_alpha[:] = -np.inf  # numba bugfix instead of log_alpha.fill(-np.inf)
    if prediction_len > 1 or extended_targets_len == 1:
        log_alpha[0, 0] = logits[0, extended_targets[0]]
    if extended_targets_len > 1:
        log_alpha[1, 0] = logits[0, extended_targets[1]]

    last_blank_indices = np.zeros(max_gram_length, dtype=np.int64)
    for t in range(1, prediction_len):  # timesteps
        start = max(0, extended_targets_len - 2 * (prediction_len - t) - max_gram_length + 1)  # addition for ngrams?
        end = min(t * 2 + 2 + max_gram_length - 1, extended_targets_len)  # ???
        log_alpha[start:end, t] = log_alpha[start:end, t - 1]
        last_blank_indices[:] = -1
        for j in range(start, end):
            current_label = extended_targets[j]
            if current_label == blank_idx:
                if last_blank_indices[-1] >= 0:
                    for k in range(last_blank_indices[-1] + 1, j):
                        log_alpha[j, t] = log_sum_exp(log_alpha[j, t], log_alpha[k, t - 1])
                last_blank_indices[:-1] = last_blank_indices[1:]  # shift
                last_blank_indices[-1] = j
            else:  # current_label != blank_idx
                raise NotImplementedError
                if j - 2 >= 0 and extended_targets[j - 2] != current_label:
                    log_alpha[j, t] = log_sum_exp(log_alpha[j, t], log_alpha[j - 2, t - 1])
            log_alpha[j, t] += logits[t, current_label]

    loss_forward = log_alpha[extended_targets_len - 1, prediction_len - 1]

    if extended_targets_len > 1:
        i = extended_targets_len - 2
        while i >= 0 and extended_targets[i] != blank_idx:
            loss_forward = log_sum_exp(loss_forward, log_alpha[i, prediction_len - 1])
            i -= 1

    # backward - beta
    log_beta = np.zeros((extended_targets_len, prediction_len))
    log_beta[:] = -np.inf  # numba bugfix instead of log_beta.fill(-np.inf)
    if prediction_len > 1 or extended_targets_len == 1:
        log_beta[extended_targets_len - 1, prediction_len - 1] = 0
    if extended_targets_len > 1:
        raise NotImplementedError  # last ngrams find?
        log_beta[extended_targets_len - 2, prediction_len - 1] = 0
    for t in range(prediction_len - 2, -1, -1):  # timesteps
        start = max(0, extended_targets_len - 2 * (prediction_len - t))
        end = min(t * 2 + 2, extended_targets_len)
        for j in range(start, end):
            current_label = extended_targets[j]
            log_beta[j, t] = log_beta[j, t + 1] + logits[t + 1, extended_targets[j]]
            if j < extended_targets_len - 1:
                log_beta[j, t] = log_sum_exp(log_beta[j, t],
                                             log_beta[j + 1, t + 1] + logits[t + 1, extended_targets[j + 1]])
                if current_label != blank_idx and j + 2 < extended_targets_len and extended_targets[
                    j + 2] != current_label:
                    log_beta[j, t] = log_sum_exp(log_beta[j, t], log_beta[j + 2, t + 1] + logits[
                        t + 1, extended_targets[j + 2]])

    alpha_beta = log_alpha + log_beta

    prob_sum = np.zeros((prediction_len, num_labels))
    prob_sum[:] = -np.inf
    for i in range(extended_targets_len):
        current_label = extended_targets[i]
        prob_sum[:, current_label] = log_sum_exp(prob_sum[:, current_label], alpha_beta[i, :])
    negative_term = prob_sum - loss_forward
    grad = np.exp(logits) - np.exp(negative_term)

    return -loss_forward, grad


def _gram_ctc_3d_loss(logits, targets, logits_lengths, targets_length, grams, blank_idx=0):
    batch_size = len(targets_length)
    grads = np.zeros_like(logits)

    losses = np.zeros(batch_size)

    # parallel computation, threading - because gil is released with numba.jin(nogil=True)
    que = queue.Queue()
    threads = []
    for i in range(batch_size):
        t = threading.Thread(target=lambda q, i, *args: q.put((i, _gram_ctc_loss(*args))),
                             args=(que, i, logits[i, :logits_lengths[i], :],
                                   targets[i, :targets_length[i]], grams, blank_idx))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    while not que.empty():
        i, (loss, grad) = que.get()
        grads[i, :logits_lengths[i], :] = grad
        losses[i] = loss

    # iterative computation
    # for i in range(batch_size):
    #     loss, grad = _ctc_loss(inputs[:input_sizes[i], i, :], targets_flat[targets_sizes_start[i]:targets_sizes_end[i]])
    #     grads[:input_sizes[i], i, :] = grad
    #     losses[i] = loss

    return losses, grads


class GramCTCLossFunction(Function):
    @staticmethod
    def forward(ctx, logits, targets, logits_lengths, targets_lengths, grams, blank_idx=0):
        # inputs: expected shape of seqLength x batchSize x alphabet_size, after logsoftmax!
        loss, grads = _gram_ctc_3d_loss(
            logits.cpu().numpy(), targets.cpu().numpy(),
            logits_lengths.cpu().numpy(), targets_lengths.cpu().numpy(),
            grams, blank_idx)
        ctx.grads = torch.FloatTensor(grads)  # save for backward not works!
        if logits.is_cuda:
            return torch.FloatTensor(loss).cuda(logits.get_device())
        return torch.FloatTensor(loss)

    @staticmethod
    def backward(ctx, grad_output):
        """
        :param grad_output: [batch_size]
        :return:
        """
        loss_grads = Variable(ctx.grads)
        if grad_output.is_cuda:
            loss_grads = loss_grads.cuda(grad_output.get_device())
        grad = loss_grads.contiguous() * grad_output.contiguous().view(-1, 1, 1)
        return grad, None, None, None, None, None


if __name__ == "__main__":
    from torch.autograd import gradcheck

    # gradchek takes a tuple of tensor as input, check if your gradient
    # evaluated with these tensors are close enough to numerical
    # approximations and returns True if they all verify this condition.
    # alphabet_size = 30
    # max_targets_len = 50
    # max_sequence_len = 100
    # batch_size = 2

    alphabet_size = 5
    max_targets_len = 100
    max_sequence_len = 200
    batch_size = 2

    np.random.seed(523)

    targets_sizes = np.random.randint(1, max_targets_len + 1, batch_size)
    inputs_sizes = targets_sizes + np.random.randint(0, (max_sequence_len - max_targets_len) + 1, batch_size)
    inputs = np.random.randn(max_sequence_len, batch_size, alphabet_size + 1)
    # expected shape seqLength x batchSize x alphabet_size

    sum_target_len = np.sum(targets_sizes)
    targets_flat = (1 + np.random.rand(sum_target_len) * alphabet_size).astype(np.int64)
    # print(targets_flat, inputs.shape, inputs)

    input = (nn.LogSoftmax(dim=2)(Variable(torch.FloatTensor(inputs), requires_grad=True)),
             Variable(torch.LongTensor(targets_flat), requires_grad=False),
             Variable(torch.LongTensor(inputs_sizes), requires_grad=False),
             Variable(torch.LongTensor(targets_sizes), requires_grad=False))
    print(GramCTCLossFunction.apply(*input).data[0])
    test = gradcheck(GramCTCLossFunction.apply, input)  # , atol=1e-5, rtol=1e-5)
    print(test)
