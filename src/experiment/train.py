import logging
import time
import torch
from torch.optim import Adam, lr_scheduler
import torch.nn.functional as F

import numpy as np

from ..data_ops.load_dataset import load_train_dataset
from ..data_ops.wrapping import unwrap
from ..data_ops.data_loaders import LeafJetLoader
from ..data_ops.data_loaders import TreeJetLoader

from ..misc.constants import *
import src.misc.schedulers as schedulers
from ..admin import ExperimentHandler

from ..monitors.meta import Collect
from ..loading.model import build_model

def train(args):
    t_start = time.time()

    eh = ExperimentHandler(**vars(args))

    ''' DATA '''
    '''----------------------------------------------------------------------- '''
    intermediate_dir, data_filename = DATASETS[args.dataset]
    data_dir = os.path.join(args.data_dir, intermediate_dir)
    #X_train, y_train, X_valid, y_valid, w_valid = prepare_train_data(args.data_dir, data_filename, args.n_train, args.n_valid, args.pileup)
    train_dataset, valid_dataset = load_train_dataset(data_dir, data_filename, args.n_train, args.n_valid, args.pileup, args.pp)

    if args.model in ['recs', 'recg']:
        DataLoader = TreeJetLoader
    else:
        DataLoader = LeafJetLoader
    train_data_loader = DataLoader(train_dataset, batch_size = args.batch_size, dropout=args.dropout, permute_particles=args.permute_particles)
    valid_data_loader = DataLoader(valid_dataset, batch_size = args.batch_size, dropout=args.dropout, permute_particles=args.permute_particles)

    ''' MODEL '''
    '''----------------------------------------------------------------------- '''
    model, settings = build_model(args.load, args.restart, args, logger=eh.stats_logger)
    eh.signal_handler.set_model(model)

    ''' OPTIMIZER AND LOSS '''
    '''----------------------------------------------------------------------- '''
    logging.info('***********')
    logging.info("Building optimizer...")

    scheduler_name = args.sched
    if scheduler_name == 'none':
        Scheduler = lr_scheduler.ExponentialLR
        sched_kwargs = dict(gamma=1)
    elif scheduler_name == 'm1':
        Scheduler = lr_scheduler.MultiStepLR
        sched_kwargs = dict(milestones=[5,10,15,20,30,40,50,60,70,80,90], gamma=args.decay)
    elif scheduler_name == 'm2':
        Scheduler = lr_scheduler.MultiStepLR
        sched_kwargs = dict(milestones=[10,20,30,40,50,60,70,80,90], gamma=args.decay)
    elif scheduler_name == 'm3':
        Scheduler = lr_scheduler.MultiStepLR
        sched_kwargs = dict(milestones=[30,60], gamma=args.decay)
    elif scheduler_name == 'exp':
        Scheduler = lr_scheduler.ExponentialLR
        sched_kwargs = dict(gamma=args.decay)
    elif scheduler_name == 'cos':
        Scheduler = schedulers.CosineAnnealingLR
        T_max = 3 if args.debug else args.period / 2
        sched_kwargs = dict(eta_min=args.lr, T_max=T_max)
        settings['lr']=0.
    elif scheduler_name == 'trap':
        Scheduler = schedulers.Piecewise
        i = 1 if args.debug else args.period
        sched_kwargs = dict(milestones=[i, args.epochs-i, args.epochs], lrs=[args.lr, args.lr, args.lr_min])
        settings['lr']=args.lr_min
    elif scheduler_name == 'lin-osc':
        Scheduler = schedulers.Piecewise
        #i = 1 if args.debug else 10
        m = args.period
        sched_kwargs = dict(milestones=[i * m for i in range(1, m+1)], lrs=[args.lr_min] + [args.lr,args.lr_min] * int(m//2))
        settings['lr']=args.lr_min
    elif scheduler_name == 'damp':
        Scheduler = schedulers.Piecewise
        #i = 1 if args.debug else 10
        m = args.period
        n_waves = args.epochs // args.period
        lr_lists = [[args.lr * 2 ** (-i),args.lr_min] for i in range(int(n_waves//2))]
        sched_kwargs = dict(milestones=[i * m for i in range(1, n_waves+1)], lrs=[args.lr_min] + [x for l in lr_lists for x in l] )
        settings['lr']=args.lr_min
    elif scheduler_name == 'lin':
        Scheduler = schedulers.Linear
        #i = 1 if args.debug else 10
        sched_kwargs = dict(start_lr=args.lr, end_lr=args.lr_min, interval_length=args.epochs)
        #args.lr=0.

    else:
        raise ValueError("bad scheduler name: {}".format(scheduler_name))
    lr_monitor = Collect('lr', fn='last')
    lr_monitor.initialize(None, eh.stats_logger.plotsdir)

    logging.info('***********')
    logging.info('Scheduler is {}'.format(scheduler_name))
    for k, v in sched_kwargs.items(): logging.info('{}: {}'.format(k, v))
    logging.info('***********')

    optimizer = Adam(model.parameters(), lr=settings['lr'], weight_decay=args.reg)

    scheduler = Scheduler(optimizer, **sched_kwargs)

    def loss(y_pred, y):
        return F.binary_cross_entropy(y_pred.squeeze(1), y)

    def callback(epoch, model, train_loss):

            t0 = time.time()
            model.eval()

            valid_loss = []
            yy, yy_pred = [], []
            for i, (x, y) in enumerate(valid_data_loader):
                y_pred = model(x)
                vl = unwrap(loss(y_pred, y)); valid_loss.append(vl)
                yv = unwrap(y); y_pred = unwrap(y_pred)
                yy.append(yv); yy_pred.append(y_pred)

            valid_loss = np.mean(np.array(valid_loss))
            yy = np.concatenate(yy, 0)
            yy_pred = np.concatenate(yy_pred, 0)

            t1=time.time()
            #import ipdb; ipdb.set_trace()
            logdict = dict(
                epoch=epoch,
                iteration=iteration,
                yy=yy,
                yy_pred=yy_pred,
                w_valid=valid_dataset.weights,
                train_loss=train_loss,
                valid_loss=valid_loss,
                settings=settings,
                model=model,
                logtime=0,
                time=((t1-t_start))
            )

            #scheduler.step(valid_loss)
            model.train()
            return logdict

    ''' TRAINING '''
    '''----------------------------------------------------------------------- '''
    eh.save(model, settings)
    logging.warning("Training...")
    iteration=1
    n_batches = len(train_data_loader)
    train_losses = []

    for i in range(args.epochs):
        logging.info("epoch = %d" % i)
        lr = scheduler.get_lr()[0]
        logging.info("lr = %.8f" % lr)
        lr_monitor(lr=lr)
        t0 = time.time()
        for j, (x, y) in enumerate(train_data_loader):
            iteration += 1

            model.train()
            optimizer.zero_grad()
            y_pred = model(x, logger=eh.stats_logger, epoch=i, iters=j, iters_left=n_batches-j-1)
            l = loss(y_pred, y)
            l.backward()
            #for w in model.parameters():
            #    print(w.grad)
            #import ipdb; ipdb.set_trace()
            train_losses.append(unwrap(l))
            if args.clip is not None:
                torch.nn.utils.clip_grad_norm(model.parameters(), args.clip)
            optimizer.step()

            ''' VALIDATION '''
            '''----------------------------------------------------------------------- '''
            if iteration % n_batches == 0:
                train_loss = np.mean(train_losses)
                logdict = callback(i, model, train_loss)
                eh.log(**logdict)
                train_losses = []
                lr_monitor.visualize('lr')

        t1 = time.time()
        logging.info("Epoch took {} seconds".format(t1-t0))

        scheduler.step()


        if t1 - t_start > args.experiment_time - 60:
            break

    eh.finished()
