import math
import numpy as np

import torch
import torch.nn.functional as F
from torch import nn as nn
from torch.autograd import Variable

SUPPORTED_LOSSES = ['ce', 'bce', 'wce', 'pce',
                    'dice', 'gdl', 'EntropyLoss', 'focal']


def compute_per_channel_dice(input, target, epsilon=1e-5, ignore_index=None, weight=None):
    # assumes that input is a normalized probability

    # input and target shapes must match
    # if target.dim() == 4:
    #     target = expand_as_one_hot(target, C=input.size()[1], ignore_index=ignore_index)

    assert input.size() == target.size(), "'input' and 'target' must have the same shape"

    # mask ignore_index if present
    if ignore_index is not None:
        mask = target.clone().ne_(ignore_index)
        mask.requires_grad = False

        input = input * mask
        target = target * mask

    input = flatten(input)
    target = flatten(target)

    target = target.float()
    # Compute per channel Dice Coefficient
    intersect = (input * target).sum(-1)
    if weight is not None:
        intersect = weight * intersect

    denominator = (input + target).sum(-1)
    return 2. * intersect / denominator.clamp(min=epsilon)


class EntropyLoss(nn.Module):
    def __init__(self, epsilon=1e-5, weight=None, ignore_index=None, sigmoid_normalization=True):
        super(EntropyLoss, self).__init__()
        self.epsilon = epsilon
        self.register_buffer('weight', weight)
        self.ignore_index = ignore_index

        if sigmoid_normalization:
            self.normalization = nn.Sigmoid()
        else:
            self.normalization = nn.Softmax(dim=1)

    def forward(self, input, target):
        # get probabilities from logits
        input = self.normalization(input)

        entropyloss = (- input * input.log()).sum()

        return entropyloss


class DiceCoefficient:
    """Computes Dice Coefficient.
    Generalized to multiple channels by computing per-channel Dice Score
    (as described in https://arxiv.org/pdf/1707.03237.pdf) and then simply taking the average.
    Input is expected to be probabilities instead of logits.
    """

    def __init__(self, epsilon=1e-5, ignore_index=None):
        self.epsilon = epsilon
        self.ignore_index = ignore_index

    def __call__(self, input, target):
        # Average across channels in order to get the final score
        return torch.mean(compute_per_channel_dice(input, target, epsilon=self.epsilon, ignore_index=self.ignore_index))


class DiceAccuracy:
    def __init__(self, shrehold=0.5, epsilon=1e-8):
        self.epsilon = epsilon
        self.shrehold = shrehold
        self.mae = 0
        self.num = 0

    def __call__(self, output, target):
        self.num += 1

        self.mae += np.sum(np.abs(output - target)) / (256 * 256)
        output = np.rint(output)
        A = output.sum()
        B = target.sum()
        I = (output * target).sum()
        # print('MAE:', self.mae / self.num)

        return A, B, I


class DiceLoss(nn.Module):
    """Computes Dice Loss, which just 1 - DiceCoefficient described above.
    Additionally allows per-class weights to be provided.
    """

    def __init__(self, epsilon=1e-5, weight=None, ignore_index=None, sigmoid_normalization=True):
        super(DiceLoss, self).__init__()
        self.epsilon = epsilon
        self.register_buffer('weight', weight)
        self.ignore_index = ignore_index
        # The output from the network during training is assumed to be un-normalized probabilities and we would
        # like to normalize the logits. Since Dice (or soft Dice in this case) is usually used for binary data,
        # normalizing the channels with Sigmoid is the default choice even for multi-class segmentation problems.
        # However if one would like to apply Softmax in order to get the proper probability distribution from the
        # output, just specify sigmoid_normalization=False.
        if sigmoid_normalization:
            self.normalization = nn.Sigmoid()
        else:
            self.normalization = nn.Softmax(dim=1)

    def forward(self, input, target):
        # get probabilities from logits
        # input = self.normalization(input)
        if self.weight is not None:
            weight = Variable(self.weight, requires_grad=False)
        else:
            weight = None

        per_channel_dice = compute_per_channel_dice(input, target, epsilon=self.epsilon, ignore_index=self.ignore_index,
                                                    weight=weight)
        # Average the Dice score across all channels/classes
        return torch.mean(1. - per_channel_dice)


class GeneralizedDiceLoss(nn.Module):
    """Computes Generalized Dice Loss (GDL) as described in https://arxiv.org/pdf/1707.03237.pdf
    """

    def __init__(self, epsilon=1e-5, weight=None, ignore_index=None, sigmoid_normalization=True):
        super(GeneralizedDiceLoss, self).__init__()
        self.epsilon = epsilon
        self.register_buffer('weight', weight)
        self.ignore_index = ignore_index
        if sigmoid_normalization:
            self.normalization = nn.Sigmoid()
        else:
            self.normalization = nn.Softmax(dim=1)

    def forward(self, input, target):
        # get probabilities from logits
        input = self.normalization(input)
        # input and target shapes must match
        # if target.dim() == 4:
        #     target = expand_as_one_hot(target, C=input.size()[1], ignore_index=self.ignore_index)

        assert input.size() == target.size(), "'input' and 'target' must have the same shape"

        # mask ignore_index if present
        if self.ignore_index is not None:
            mask = target.clone().ne_(self.ignore_index)
            mask.requires_grad = False

            input = input * mask
            target = target * mask

        input = flatten(input)
        target = flatten(target)

        target = target.float()
        target_sum = target.sum(-1)
        class_weights = Variable(
            1. / (target_sum * target_sum).clamp(min=self.epsilon), requires_grad=False)

        intersect = (input * target).sum(-1) * class_weights
        if self.weight is not None:
            weight = Variable(self.weight, requires_grad=False)
            intersect = weight * intersect

        denominator = (input.sum(-1) * class_weights + target.sum(-1))

        return torch.mean(1. - 2. * intersect / denominator.clamp(min=self.epsilon))


class WeightedCrossEntropyLoss(nn.Module):
    """WeightedCrossEntropyLoss (WCE) as described in https://arxiv.org/pdf/1707.03237.pdf
    """

    def __init__(self, weight=None, ignore_index=-1):
        super(WeightedCrossEntropyLoss, self).__init__()
        self.register_buffer('weight', weight)
        self.ignore_index = ignore_index

    def forward(self, input, target):
        class_weights = self._class_weights(input)
        if self.weight is not None:
            weight = Variable(self.weight, requires_grad=False)
            class_weights = class_weights * weight
        return F.cross_entropy(input, target, weight=class_weights, ignore_index=self.ignore_index)

    @staticmethod
    def _class_weights(input):
        # normalize the input first
        input = F.softmax(input, dim=1, _stacklevel=5)
        flattened = flatten(input)
        nominator = (1. - flattened).sum(-1)
        denominator = flattened.sum(-1)
        class_weights = Variable(nominator / denominator, requires_grad=False)
        return class_weights


class IgnoreIndexLossWrapper:
    """
    Wrapper around loss functions which do not support 'ignore_index', e.g. BCELoss.
    Throws exception if the wrapped loss supports the 'ignore_index' option.
    """

    def __init__(self, loss_criterion, ignore_index=-1):
        if hasattr(loss_criterion, 'ignore_index'):
            raise RuntimeError(
                "Cannot wrap {type(loss_criterion)}. Use 'ignore_index' attribute instead")
        self.loss_criterion = loss_criterion
        self.ignore_index = ignore_index

    def __call__(self, input, target):
        # always expand target tensor, so that input.size() == target.size()
        if target.dim() == 4:
            target = expand_as_one_hot(target, C=input.size()[
                                       1], ignore_index=self.ignore_index)

        assert input.size() == target.size()

        mask = target.clone().ne_(self.ignore_index)
        mask.requires_grad = False

        masked_input = input * mask
        masked_target = target * mask
        return self.loss_criterion(masked_input, masked_target)


class PixelWiseCrossEntropyLoss(nn.Module):
    def __init__(self, class_weights=None, ignore_index=None):
        super(PixelWiseCrossEntropyLoss, self).__init__()
        self.register_buffer('class_weights', class_weights)
        self.ignore_index = ignore_index
        self.log_softmax = nn.LogSoftmax(dim=1)
        self.sigmoid = nn.LogSigmoid()

    def forward(self, input, target, weights):
        assert target.size() == weights.size()
        # normalize the input
        log_probabilities = self.log_softmax(input)
        weights = self.sigmoid(weights)
        # weights在-1到0之间
        # standard CrossEntropyLoss requires the target to be (NxDxHxW), so we need to expand it to (NxCxDxHxW)
        target = expand_as_one_hot(target, C=input.size()[
                                   1], ignore_index=self.ignore_index)
        # expand weights
        weights = weights.unsqueeze(0)
        weights = weights.expand_as(input)
        # weights[:,0,:,:,:] = 1 - weights[:,0,:,:,:]
        # weight是负的，最小说明越难分
        # print(weights.min())

        # mask ignore_index if present
        # if self.ignore_index is not None:
        #     mask = Variable(target.data.ne(self.ignore_index).float(), requires_grad=False)
        #     log_probabilities = log_probabilities * mask
        #     target = target * mask

        # apply class weights
        if self.class_weights is None:
            class_weights = torch.ones(
                input.size()[1]).float().to(input.device)
        else:
            class_weights = self.class_weights

        class_weights = class_weights.view(1, input.size()[1], 1, 1, 1)
        class_weights = Variable(class_weights, requires_grad=False)
        # add class_weights to each channel
        # weight是正的,越小说明越难分
        weights += class_weights

        weights = 1 / weights
        weights[weights > 10] = 10

        # compute the losses
        result = -weights * target * log_probabilities
        # print(result.mean())
        # average the losses
        return result.mean()


# class PixelWiseCrossEntropyLoss(nn.Module):
#     def __init__(self, class_weights=None, ignore_index=None):
#         super(PixelWiseCrossEntropyLoss, self).__init__()
#         self.register_buffer('class_weights', class_weights)
#         self.ignore_index = ignore_index
#         self.log_softmax = nn.LogSoftmax(dim=1)
#
#     def forward(self, input, target, weights):
#         assert target.size() == weights.size()
#         # normalize the input
#         log_probabilities = self.log_softmax(input)
#         # standard CrossEntropyLoss requires the target to be (NxDxHxW), so we need to expand it to (NxCxDxHxW)
#         target = expand_as_one_hot(target, C=input.size()[1], ignore_index=self.ignore_index)
#         # expand weights
#         weights = weights.unsqueeze(0)
#         weights = weights.expand_as(input)
#
#         # mask ignore_index if present
#         if self.ignore_index is not None:
#             mask = Variable(target.data.ne(self.ignore_index).float(), requires_grad=False)
#             log_probabilities = log_probabilities * mask
#             target = target * mask
#
#         # apply class weights
#         if self.class_weights is None:
#             class_weights = torch.ones(input.size()[1]).float().to(input.device)
#         else:
#             class_weights = self.class_weights
#         class_weights = class_weights.view(1, input.size()[1], 1, 1, 1)
#         class_weights = Variable(class_weights, requires_grad=False)
#         # add class_weights to each channel
#         weights = class_weights + weights
#
#         # compute the losses
#         result = -weights * target * log_probabilities
#         # average the losses
#         return result.mean()


def flatten(tensor):
    """Flattens a given tensor such that the channel axis is first.
    The shapes are transformed as follows:
       (N, C, D, H, W) -> (C, N * D * H * W)
    """
    C = tensor.size(1)
    # new axis order
    axis_order = (1, 0) + tuple(range(2, tensor.dim()))
    # Transpose: (N, C, D, H, W) -> (C, N, D, H, W)
    transposed = tensor.permute(axis_order)
    # Flatten: (C, N, D, H, W) -> (C, N * D * H * W)
    return transposed.view(C, -1)


def expand_as_one_hot(input, C, ignore_index=None):
    """
    Converts NxDxHxW label image to NxCxDxHxW, where each label is stored in a separate channel
    :param input: 4D input image (NxDxHxW)
    :param C: number of channels/labels
    :param ignore_index: ignore index to be kept during the expansion
    :return: 5D output image (NxCxDxHxW)
    """
    assert input.dim() == 4

    shape = input.size()
    shape = list(shape)
    shape.insert(1, C)
    shape = tuple(shape)

    # expand the input tensor to Nx1xDxHxW
    src = input.unsqueeze(0)

    if ignore_index is not None:
        # create ignore_index mask for the result
        expanded_src = src.expand(shape)
        mask = expanded_src == ignore_index
        # clone the src tensor and zero out ignore_index in the input
        src = src.clone()
        src[src == ignore_index] = 0
        # scatter to get the one-hot tensor
        result = torch.zeros(shape).to(input.device).scatter_(1, src, 1)
        # bring back the ignore_index in the result
        result[mask] = ignore_index
        return result
    else:
        # scatter to get the one-hot tensor
        return torch.zeros(shape).to(input.device).scatter_(1, src, 1)


class FocalLoss(nn.Module):
    r"""
        This criterion is a implemenation of Focal Loss, which is proposed in
        Focal Loss for Dense Object Detection.

            Loss(x, class) = - \alpha (1-softmax(x)[class])^gamma \log(softmax(x)[class])

        The losses are averaged across observations for each minibatch.
        Args:
            alpha(1D Tensor, Variable) : the scalar factor for this criterion
            gamma(float, double) : gamma > 0; reduces the relative loss for well-classi?ed examples (p > .5),
                                   putting more focus on hard, misclassi?ed examples
            size_average(bool): size_average(bool): By default, the losses are averaged over observations for each minibatch.
                                However, if the field size_average is set to False, the losses are
                                instead summed for each minibatch.
    """

    def __init__(self, weight=None, gamma=2, ignore_index=-1, size_average=True):
        super(FocalLoss, self).__init__()
        self.weight = weight
        if weight is not None:
            weight = Variable(self.weight, requires_grad=False)
        self.gamma = gamma
        self.nll_loss = torch.nn.NLLLoss(weight=self.weight)

    def forward(self, inputs, targets):
        return self.nll_loss((1 - F.softmax(inputs, 1)) ** self.gamma * F.log_softmax(inputs, 1), targets)


def get_loss_criterion(loss_str, weight=None, ignore_index=None):
    """
    Returns the loss function based on the loss_str.
    :param loss_str: specifies the loss function to be used
    :param final_sigmoid: used only with Dice-based losses. If True the Sigmoid normalization will be applied
        before computing the loss otherwise it will use the Softmax.
    :param weight: a manual rescaling weight given to each class
    :param ignore_index: specifies a target value that is ignored and does not contribute to the input gradient
    :return: an instance of the loss function
    """
    assert loss_str in SUPPORTED_LOSSES, 'Invalid loss string: {}'.format(
        loss_str)
    if loss_str == 'bce':
        if ignore_index is None:
            return nn.BCEWithLogitsLoss()
        else:
            return IgnoreIndexLossWrapper(nn.BCEWithLogitsLoss(), ignore_index=ignore_index)
    elif loss_str == 'ce':
        if ignore_index is None:
            ignore_index = -100  # use the default 'ignore_index' as defined in the CrossEntropyLoss
        return nn.CrossEntropyLoss(weight=weight, ignore_index=ignore_index)
    elif loss_str == 'wce':
        if ignore_index is None:
            ignore_index = -100  # use the default 'ignore_index' as defined in the CrossEntropyLoss
        return WeightedCrossEntropyLoss(weight=weight, ignore_index=ignore_index)
    elif loss_str == 'pce':
        return PixelWiseCrossEntropyLoss(class_weights=weight, ignore_index=ignore_index)
    elif loss_str == 'gdl':
        return GeneralizedDiceLoss(weight=weight, ignore_index=ignore_index)
    elif loss_str == 'dice':
        return DiceLoss(weight=weight, ignore_index=ignore_index)
    elif loss_str == 'focal':
        return FocalLoss(weight=weight, ignore_index=ignore_index)
    else:
        return EntropyLoss(weight=weight, ignore_index=ignore_index)
