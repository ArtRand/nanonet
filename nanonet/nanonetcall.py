#!/usr/bin/env python
import argparse
import json
import os
import re
import math
import sys
import shutil
import tempfile
import timeit
import subprocess
import pkg_resources
import itertools
import numpy as np
import pyopencl as cl
from functools import partial

from nanonet import decoding, nn
from nanonet.fast5 import Fast5, iterate_fast5, short_names
from nanonet.util import random_string, conf_line, FastaWrite, tang_imap, all_nmers, kmers_to_sequence, kmer_overlap, group_by_list, AddFields
from nanonet.cmdargs import FileExist, CheckCPU, AutoBool
from nanonet.features import make_basecall_input_multi
from nanonet.jobqueue import JobQueue

import warnings
warnings.simplefilter("ignore")

now = timeit.default_timer

__fast5_analysis_name__ = 'Basecall_RNN_1D'
__fast5_section_name__ = 'BaseCalled_{}'
__ETA__ = 1e-300


def get_parser():
    parser = argparse.ArgumentParser(
        description="""A simple RNN basecaller for Oxford Nanopore data.""",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument("input", action=FileExist,
        help="A path to fast5 files.")
    parser.add_argument("--watch", default=None, type=int,
        help="Switch to watching folder, argument value used as timeout period.")
    parser.add_argument("--section", default=None, choices=('template', 'complement'),
        help="Section of read for which to produce basecalls, will override that stored in model file.")
    parser.add_argument("--event_detect", default=True, action=AutoBool,
        help="Perform event detection, else use existing event data")

    parser.add_argument("--output", type=str,
        help="Output name, output will be in fasta format.")
    parser.add_argument("--write_events", action=AutoBool, default=False,
        help="Write event datasets to .fast5.")
    parser.add_argument("--strand_list", default=None, action=FileExist,
        help="List of reads to process.")
    parser.add_argument("--limit", default=None, type=int,
        help="Limit the number of input for processing.")
    parser.add_argument("--min_len", default=500, type=int,
        help="Min. read length (events) to basecall.")
    parser.add_argument("--max_len", default=15000, type=int,
        help="Max. read length (events) to basecall.")

    parser.add_argument("--model", type=str, action=FileExist,
        default=pkg_resources.resource_filename('nanonet', 'data/default_template.npy'),
        help="Trained ANN.")
    parser.add_argument("--jobs", default=1, type=int, action=CheckCPU,
        help="No of decoding jobs to run in parallel.")

    parser.add_argument("--trans", type=float, nargs=3, default=None,
        metavar=('stay', 'step', 'skip'), help='Base transition probabilities')
    parser.add_argument("--fast_decode", action=AutoBool, default=False,
        help="Use simple, fast decoder with no transition estimates.")

    parser.add_argument("--exc_opencl", action=AutoBool, default=False,
        help="Do not use CPU alongside OpenCL, overrides --jobs.")
    parser.add_argument("--list_platforms", action=AutoBool, default=False,
        help="Output list of available OpenCL GPU platforms.")
    parser.add_argument("--platforms", nargs="+", type=str,
        help="List of OpenCL GPU platforms and devices to be used in a format VENDOR:DEVICE:N_Files space separated, i.e. --platforms nvidia:0:1 amd:0:2 amd:1:2.")

    return parser

class ProcessAttr(object):
    def __init__(self, use_opencl=False, vendor=None, device_id=0):
        self.use_opencl = use_opencl
        self.vendor = vendor
        self.device_id = device_id

def list_opencl_platforms():
    print('\n' + '=' * 60 + '\nOpenCL Platforms and Devices')
    platforms = [p for p in cl.get_platforms() if p.get_devices(device_type=cl.device_type.GPU)]
    for platform in platforms:
        print('=' * 60)
        print('Platform - Name:  ' + platform.name)
        print('Platform - Vendor:  ' + platform.vendor)
        print('Platform - Version:  ' + platform.version)
        for device in platform.get_devices(device_type=cl.device_type.GPU):  # Print each device per-platform
            print('    ' + '-' * 56)
            print('    Device - Name:  ' + device.name)
            print('    Device - Type:  ' + cl.device_type.to_string(device.type))
            print('    Device - Max Clock Speed:  {0} Mhz'.format(device.max_clock_frequency))
            print('    Device - Compute Units:  {0}'.format(device.max_compute_units))
            print('    Device - Local Memory:  {0:.0f} KB'.format(device.local_mem_size/1024))
            print('    Device - Constant Memory:  {0:.0f} KB'.format(device.max_constant_buffer_size/1024))
            print('    Device - Global Memory: {0:.0f} GB'.format(device.global_mem_size/1073741824.0))
    print('\n')


def process_read(modelfile, fast5, min_prob=1e-5, trans=None, post_only=False, write_events=True, fast_decode=False, **kwargs):
    """Run neural network over a set of fast5 files

    :param modelfile: neural network specification.
    :param fast5: read file to process
    :param post_only: return only the posterior matrix
    :param **kwargs: kwargs of make_basecall_input_multi
    """
    #sys.stderr.write("CPU process\n processing {}\n".format(fast5))

    network = np.load(modelfile).item()
    kwargs['window'] = network.meta['window']

    # Get features
    try:
        it = make_basecall_input_multi((fast5,), **kwargs)
        if write_events:
            fname, features, events = it.next()
        else:
            fname, features, _ = it.next()
    except Exception as e:
        return None

    # Run network
    t0 = now()
    post = network.run(features.astype(nn.dtype))
    network_time = now() - t0

    # Manipulate posterior matrix
    post, good_events = clean_post(post, network.meta['kmers'], min_prob)
    if post_only:
        return post

    # Decode kmers
    t0 = now()
    if fast_decode:
        score, states = decoding.decode_homogenous(post, log=False)
    else:
        trans = decoding.fast_estimate_transitions(post, trans=trans)
        score, states = decoding.decode_profile(post, trans=np.log(__ETA__ + trans), log=False)
    decode_time = now() - t0

    # Form basecall
    kmers = network.meta['kmers']
    kmer_path = [kmers[i] for i in states]
    seq = kmers_to_sequence(kmer_path)

    # Write events table
    if write_events:
        write_to_file(fast5, events, kwargs['section'], seq, good_events, kmer_path, kmers, post, states)

    return (fname, seq, score, len(features)), (network_time, decode_time)


def clean_post(post, kmers, min_prob):
    # Do we have an XXX kmer? Strip out events where XXX most likely,
    #    and XXX states entirely
    if kmers[-1] == 'X'*len(kmers[-1]):
        bad_kmer = post.shape[1] - 1
        max_call = np.argmax(post, axis=1)
        good_events = (max_call != bad_kmer)
        post = post[good_events]
        post = post[:, :-1]
        if len(post) == 0:
            return None, None
    
    weights = np.sum(post, axis=1).reshape((-1,1))
    post /= weights 
    post = min_prob + (1.0 - min_prob) * post
    return post, good_events


def write_to_file(fast5, events, section, seq, good_events, kmer_path, kmers, post, states):
    adder = AddFields(events[good_events])
    adder.add('model_state', kmer_path,
        dtype='>S{}'.format(len(kmers[0])))
    adder.add('p_model_state', np.fromiter(
        (post[i,j] for i,j in itertools.izip(xrange(len(post)), states)),
        dtype=float, count=len(post)))
    adder.add('mp_model_state', np.fromiter(
        (kmers[i] for i in np.argmax(post, axis=1)),
        dtype='>S{}'.format(len(kmers[0])), count=len(post)))
    adder.add('p_mp_model_state', np.max(post, axis=1))
    adder.add('move', np.array(kmer_overlap(kmer_path)), dtype=int)

    mid = len(kmers[0]) / 2
    bases = set(''.join(kmers)) - set('X')
    for base in bases:
        cols = np.fromiter((k[mid] == base for k in kmers),
            dtype=bool, count=len(kmers))
        adder.add('p_{}'.format(base), np.sum(post[:, cols], axis=1), dtype=float)

    events = adder.finalize()

    with Fast5(fast5, 'a') as fh:
       base = fh._join_path(
           fh.get_analysis_new(__fast5_analysis_name__),
           __fast5_section_name__.format(section))
       fh._add_event_table(events, fh._join_path(base, 'Events'))
       try:
           name = fh.get_read(group=True).attrs['read_id']
       except:
           name = fh.filename_short
       fh._add_string_dataset(
           '@{}\n{}\n+\n{}\n'.format(name, seq, '!'*len(seq)),
           fh._join_path(base, 'Fastq'))

        
def process_read_opencl(modelfile, pa, fast5_list, min_prob=1e-5, trans=None, write_events=True, fast_decode=False, **kwargs):
    """Run neural network over a set of fast5 files

    :param modelfile: neural network specification.
    :param fast5: read file to process
    :param post_only: return only the posterior matrix
    :param **kwargs: kwargs of make_basecall_input_multi
    """
    #sys.stderr.write("OpenCL process\n processing {}\n{}\n".format(fast5_list, pa.__dict__))

    network = np.load(modelfile).item()
    kwargs['window'] = network.meta['window']

    # Get features
    try:
        file_list, features_list, events_list = zip(*(
            make_basecall_input_multi(fast5_list, **kwargs)
        ))
    except:
        return [None] * len(fast5_list)
    features_list = [x.astype(nn.dtype) for x in features_list] 
    if not write_events:
        events_list = None
    n_files = len(file_list) # might be different for input length

    # Set up OpenCL
    platform = [
        p for p in cl.get_platforms()
        if p.get_devices(device_type=cl.device_type.GPU)
        and pa.vendor.lower() in p.get_info(cl.platform_info.NAME).lower()
    ][0]
    device = platform.get_devices(
        device_type=cl.device_type.GPU
    )[pa.device_id]
    max_workgroup_size = device.get_info(cl.device_info.MAX_WORK_GROUP_SIZE)
    ctx = cl.Context([device]) 
    queue_list = [cl.CommandQueue(ctx)] * n_files

    # Run network
    t0 = now()
    post_list = network.run(features_list, ctx, queue_list)
    network_time_list = [(now() - t0) / n_files] * n_files

    # Manipulate posterior
    post_list, good_events_list = zip(*(
        clean_post(post, network.meta['kmers'], min_prob) for post in post_list
    ))

    # Decode kmers
    t0 = now()
    if fast_decode:
        # actually this is slower, but we want to run the same algorithm
        #   in the case of heterogeneous computer resource.
        score_list, states_list = zip(*(
            decoding.decode_homogenous(post, log=False) for post in post_list
        ))
    else:
        trans_list = [np.log(__ETA__ +
            decoding.fast_estimate_transitions(post, trans=trans))
            for post in post_list]
        score_list, states_list = decoding.decode_profile_opencl(
            ctx, queue_list, post_list, trans_list=trans_list,
            log=False, max_workgroup_size=max_workgroup_size
        )
    decode_time_list = [(now() - t0) / n_files] * n_files
            
    # Form basecall
    kmers = network.meta['kmers']
    kmer_path_list = []
    seq_list = []
    for states in states_list:
        kmer_path = [kmers[i] for i in states]
        seq = kmers_to_sequence(kmer_path)
        kmer_path_list.append(kmer_path)
        seq_list.append(seq)

    # Write events table
    if write_events:
        section_list = (kwargs['section'] for _ in xrange(n_files))
        kmers_list = (network.meta['kmers'] for _ in xrange(n_files))
        for data in zip(
            file_list, events_list, section_list, seq_list,
            good_events_list, kmer_path_list, kmers_list, post_list, states_list
            ):
            write_to_file(*data)

    # Construst a sequences of objects as process_read returns
    data = zip(file_list, seq_list, score_list, (len(x) for x in features_list))
    timings = zip(network_time_list, decode_time_list)
    ret = zip(data, timings)
    if n_files < len(fast5_list):
        # pad as if failed in process_read
        ret.extend([None]*(len(fast5_list) - n_files))
    return ret


def main():
    if len(sys.argv) == 1:
        sys.argv.append("-h")
    args = get_parser().parse_args()
    
    if args.list_platforms:
        list_opencl_platforms() 
        sys.exit(0)
        
    modelfile  = os.path.abspath(args.model)
    if args.section is None:
        try:
            args.section = np.load(modelfile).item().meta['section']
        except:
            sys.stderr.write("No 'section' found in modelfile, try specifying --section.\n")
            sys.exit(1)
                 
            
    #TODO: handle case where there are pre-existing files.
    if args.watch is not None:
        # An optional component
        from nanonet.watcher import Fast5Watcher
        fast5_files = Fast5Watcher(args.input, timeout=args.watch)
    else:
        sort_by_size = 'desc' if args.platforms is not None else None
        fast5_files = iterate_fast5(args.input, paths=True, strand_list=args.strand_list, limit=args.limit, sort_by_size=sort_by_size)

    fix_args = [
        modelfile
    ]
    fix_kwargs = {a: getattr(args, a) for a in ( 
        'min_len', 'max_len', 'section',
        'event_detect', 'fast_decode',
        'write_events'
    )}
   
    workers = []
    if not args.exc_opencl:
        cpu_function = partial(process_read, *fix_args, **fix_kwargs)
        workers.extend([(cpu_function, None)] * args.jobs)
    if args.platforms is not None:
        for platform in args.platforms:
            vendor, device_id, n_files = platform.split(':')
            pa = ProcessAttr(use_opencl=True, vendor=vendor, device_id=int(device_id))
            fargs = fix_args + [pa]
            opencl_function = partial(process_read_opencl, *fargs, **fix_kwargs)
            workers.append(
                (opencl_function, int(n_files))
            )

    n_reads = 0
    n_bases = 0
    n_events = 0
    timings = [0.0, 0.0]

    # Select how to spread load
    if args.platforms is None:
        # just CPU
        worker, n_files = workers[0]
        mapper = tang_imap(worker, fast5_files, threads=args.jobs, unordered=True)
    elif len(workers) == 1:
        # single opencl device
        #    need to wrap files in lists, and unwrap results
        worker, n_files = workers[0]
        fast5_files = group_by_list(fast5_files, [n_files])
        mapper = itertools.chain.from_iterable(itertools.imap(worker, fast5_files))
    else:
        # Heterogeneous compute
        mapper = JobQueue(fast5_files, workers)

    t0 = now()
    with FastaWrite(args.output) as fasta:
        for result in mapper:
            if result is None:
                continue
            data, time = result
            fname, basecall, _, n_ev = data
            name, _ = short_names(fname) 
            fasta.write(*(name, basecall))
            n_reads += 1
            n_bases += len(basecall)
            n_events += n_ev
            timings = [x + y for x, y in zip(timings, time)]
    t1 = now()
    sys.stderr.write('Basecalled {} reads ({} bases, {} events) in {}s (wall time)\n'.format(n_reads, n_bases, n_events, t1 - t0))
    if n_reads > 0:
        network, decoding  = timings
        sys.stderr.write(
            'Run network: {:6.2f} ({:6.3f} kb/s, {:6.3f} kev/s)\n'
            'Decoding:    {:6.2f} ({:6.3f} kb/s, {:6.3f} kev/s)\n'
            .format(
                network, n_bases/1000.0/network, n_events/1000.0/network,
                decoding, n_bases/1000.0/decoding, n_events/1000.0/decoding,
            )
        )


if __name__ == "__main__":
    main()
