#!/bin/env python
# -*- coding: utf-8 -*-


"""Run hit finding on multiple cores using MPI.

Usage:
   batch_hit_finder.py <input-lst> <conf-file> <hit-dir> [options]

Options:
    -h --help               Show this screen.
    --min-peaks NUM         Specify min peaks for a hit [default: 20].
    --min-max-intensity NUM Specify min max intensity [default: 0].
    --batch-size SIZE       Specify batch size in a job [default: 10].
    --buffer-size SIZE      Specify buffer size in MPI communication
                            [default: 500000].
    --update-freq FREQ      Specify update frequency of progress [default: 10].
    --flush                 Flush output of print.
"""
from __future__ import print_function
from six import print_ as print

from mpi4py import MPI
try:
    import mkl
    mkl.set_num_threads(1)
except:
    pass

import numpy as np
import pandas as pd
import h5py
import time

import sys
import os
from shutil import copyfile
from docopt import docopt
import yaml

import util


def master_run(args):
    flush = args['--flush']
    # mkdir if not exist
    hit_dir = args['<hit-dir>']
    if not os.path.isdir(hit_dir):
        os.makedirs(hit_dir)

    input_lst = args['<input-lst>']
    min_peaks = int(args['--min-peaks'])
    with open(input_lst) as f:
        _files = f.readlines()
    files = [f.strip() for f in _files]
    # load hit finding configuration file
    conf_file = args['<conf-file>']
    with open(conf_file) as f:
        conf = yaml.load(f)
    dataset = conf['dataset']
    # save hit conf and mask in hit dir
    mask_file = conf['mask file']
    if mask_file is not None:
        copyfile(mask_file, '%s/mask.npy' % hit_dir)
    copyfile(conf_file, '%s/hit_conf.yml' % hit_dir)

    batch_size = int(args['--batch-size'])
    buffer_size = int(args['--buffer-size'])
    jobs, nb_frames = util.collect_jobs(files, dataset, batch_size)
    nb_jobs = len(jobs)
    print('%d frames, %d jobs to be processed' %
          (nb_frames, nb_jobs), flush=flush)

    update_freq = int(args['--update-freq'])
    prefix = os.path.basename(input_lst).split('.')[0]

    # dispatch jobs
    job_id = 0
    reqs = {}
    results = []
    workers = set(range(1, size))
    finished_workers = set()
    time_start = time.time()
    for worker in workers:
        if job_id < nb_jobs:
            job = jobs[job_id]
        else:
            job = []  # dummy job
        comm.isend(job, dest=worker)
        reqs[worker] = comm.irecv(buf=buffer_size, source=worker)
        print('job %d/%d --> worker %d' % (job_id, nb_jobs, worker), flush=flush)
        job_id += 1
    while job_id < nb_jobs:
        stop = False
        time.sleep(0.1)
        workers -= finished_workers
        for worker in workers:
            finished, result = reqs[worker].test()
            if finished:
                results += result
                if job_id < nb_jobs:
                    print('job %d/%d --> worker %d' %
                          (job_id, nb_jobs, worker), flush=flush)
                    comm.isend(stop, dest=worker)
                    comm.isend(jobs[job_id], dest=worker)
                    reqs[worker] = comm.irecv(buf=buffer_size, source=worker)
                    job_id += 1
                else:
                    stop = True
                    comm.isend(stop, dest=worker)
                    print('stop signal --> worker %d' % worker, flush=flush)
                    finished_workers.add(worker)
        if job_id % update_freq == 0:
            # update stat
            progress = float(job_id) / nb_jobs * 100
            df = pd.DataFrame(results)
            if len(df) == 0:
                continue
            processed_hits = len(df[df['nb_peak'] >= min_peaks])
            processed_frames = len(df)
            hit_rate = float(processed_hits) / processed_frames * 100.
            stat_dict = {
                'progress': '%.2f%%' % progress,
                'processed hits': processed_hits,
                'hit rate': '%.2f%%' % hit_rate,
                'duration/sec': 'not finished',
                'processed frames': processed_frames,
                'total jobs': nb_jobs,
                'total frames': nb_frames,
                'time start': time_start,
            }
            stat_file = os.path.join(hit_dir, 'stat.yml')
            with open(stat_file, 'w') as f:
                yaml.dump(stat_dict, f, default_flow_style=False)

    all_done = False
    while not all_done:
        time.sleep(0.1)
        all_done = True
        workers -= finished_workers
        for worker in workers:
            finished, result = reqs[worker].test()
            if finished:
                results += result
                stop = True
                print('stop signal --> worker %d' % worker, flush=flush)
                comm.isend(stop, dest=worker)
                finished_workers.add(worker)
            else:
                all_done = False
    time_end = time.time()
    duration = time_end - time_start
    # save stat file
    df = pd.DataFrame(results)
    if len(df) == 0:
        print('WARNING! Empty hit finding results!')
        sys.exit()

    processed_hits = len(df[df['nb_peak'] >= min_peaks])
    processed_frames = len(df)
    hit_rate = float(processed_hits) / processed_frames * 100.
    stat_dict = {
        'progress': 'done',
        'processed hits': processed_hits,
        'hit rate': '%.2f%%' % hit_rate,
        'duration/sec': duration,
        'processed frames': processed_frames,
        'total jobs': nb_jobs,
        'total frames': nb_frames,
        'time start': time_start,
    }
    stat_file = os.path.join(hit_dir, 'stat.yml')
    with open(stat_file, 'w') as f:
        yaml.dump(stat_dict, f, default_flow_style=False)

    # save simple results to csv
    csv_file = os.path.join(hit_dir, '%s.csv' % prefix)
    simple_results = []
    for i in range(len(results)):
        simple_results.append(
            {
                'filepath': results[i]['filepath'],
                'dataset': results[i]['dataset'],
                'frame': results[i]['frame'],
                'nb_peak': results[i]['nb_peak']
            }
        )
    df = pd.DataFrame(simple_results)
    df.to_csv(csv_file)

    # save detailed peak info to npz
    peak_file = os.path.join(hit_dir, '%s.npy' % prefix)
    np.save(peak_file, results)

    print('All Done!', flush=flush)


def worker_run(args):
    stop = False
    filepath = None
    h5_obj = None
    buffer_size = int(args['--buffer-size'])
    flush = args['--flush']
    min_max_intensity = float(args['--min-max-intensity'])

    # hit finding parameters
    with open(args['<conf-file>']) as f:
        conf = yaml.load(f)
    center = conf['center']
    adu_per_photon = conf['adu per photon']
    epsilon = conf['epsilon']
    bin_size = conf['bin size']
    if conf['mask on']:
        mask = util.read_image(conf['mask file'])['image']
    else:
        mask = None
    hit_finder = conf['hit finder']
    gaussian_sigma = conf['gaussian filter sigma']
    min_distance = conf['min distance']
    merge_flat_peaks = conf['merge flat peaks']
    min_gradient = conf['min gradient']
    max_peaks = conf['max peaks']
    min_snr = conf['min snr']
    min_pixels = conf['min pixels']
    max_pixels = conf['max pixels']
    peak_refine_mode = conf['peak refine mode']
    snr_mode = conf['snr mode']
    sig_radius = conf['signal radius']
    bg_inner_radius = conf['background inner radius']
    bg_outer_radius = conf['background outer radius']
    crop_size = conf['crop size']
    bg_ratio = conf['background ratio']
    sig_ratio = conf['signal ratio']
    sig_thres = conf['signal threshold']
    dataset = conf['dataset']

    # perform hit finding
    while not stop:
        job = comm.recv(buf=buffer_size, source=0)
        for i in range(len(job)):
            _filepath = job[i]['filepath']
            frame = job[i]['frame']
            if _filepath != filepath:
                filepath = _filepath
                ext = filepath.split('.')[-1]
                h5_obj = h5py.File(filepath, 'r') if ext in ('cxi', 'h5') \
                    else None
                lcls_data = util.get_lcls_data(filepath) if ext == 'lcls' \
                    else None
            image_data = util.read_image(
                filepath, frame=frame,
                h5_obj=h5_obj,
                lcls_data=lcls_data,
                dataset=dataset
            )
            data_dict = {}
            if ext == 'lcls':
                if 'event_codes' in image_data:
                    data_dict['event_codes'] = image_data['event_codes']
                if 'flow_rate' in image_data:
                    data_dict['flow_rate'] = image_data['flow_rate']
                if 'pressure' in image_data:
                    data_dict['pressure'] = image_data['pressure']
                if 'photon_energy' in image_data:
                    data_dict['photon_energy'] = image_data['photon_energy']
                if 'clen' in image_data:
                    data_dict['clen'] = image_data['clen']
                if 'fiducial' in image_data:
                    data_dict['fiducial'] = image_data['fiducial']
                if 'epics-PV' in image_data:
                    data_dict['epics-PV'] = image_data['epics-PV']
            elif ext == 'h5':
                if 'pump_on' in image_data:
                    data_dict['pump_on'] = image_data['pump_on']
            total_intensity, max_intensity = 0., 0.
            if image_data['image'] is not None:
                image = image_data['image'] * mask if mask is not None else image_data['image']
                total_intensity = np.sum(image)
                max_intensity = np.max(image)
            if max_intensity > min_max_intensity:
                peaks_dict = util.find_peaks(
                    image_data['image'], center,
                    adu_per_photon=adu_per_photon,
                    epsilon=epsilon,
                    bin_size=bin_size,
                    mask=mask,
                    hit_finder=hit_finder,
                    gaussian_sigma=gaussian_sigma,
                    min_gradient=min_gradient,
                    min_distance=min_distance,
                    merge_flat_peaks=merge_flat_peaks,
                    max_peaks=max_peaks,
                    min_snr=min_snr,
                    min_pixels=min_pixels,
                    max_pixels=max_pixels,
                    refine_mode=peak_refine_mode,
                    snr_mode=snr_mode,
                    signal_radius=sig_radius,
                    bg_inner_radius=bg_inner_radius,
                    bg_outer_radius=bg_outer_radius,
                    crop_size=crop_size,
                    bg_ratio=bg_ratio,
                    signal_ratio=sig_ratio,
                    signal_thres=sig_thres,
                )
            else:
                peaks_dict = {}
            if peaks_dict.get('strong', None) is not None:
                job[i]['nb_peak'] = len(peaks_dict['strong'])
                job[i]['peak_info'] = peaks_dict['info']
            else:
                job[i]['nb_peak'] = 0
            data_dict['total_intensity'] = total_intensity
            data_dict['max_intensity'] = max_intensity
            job[i]['data_dict'] = data_dict
        comm.send(job, dest=0)
        stop = comm.recv(source=0)
    print('slave %d is exiting' % rank, flush=flush)


if __name__ == '__main__':
    comm = MPI.COMM_WORLD
    size = comm.Get_size()
    if size == 1:
        print('Run batch hit finder with at least 2 processes!')
        sys.exit()

    rank = comm.Get_rank()
    argv = docopt(__doc__)
    if rank == 0:
        master_run(argv)
    else:
        worker_run(argv)
