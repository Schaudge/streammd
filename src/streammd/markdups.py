"""
Read a SAM file from STDIN, mark duplicates in a single pass and stream
processed records to STDOUT.

Input must begin with a valid SAM header, followed by qname-grouped records.
Currently only paired reads are handled.

Default log level is 'INFO' — set to something else with the LOG_LEVEL
environment variable.
"""
from importlib import metadata
from itertools import repeat
from multiprocessing import Manager, Pool, Process
from multiprocessing.managers import SharedMemoryManager
import argparse
import logging
import os
import sys
from pysam import AlignmentHeader, AlignedSegment
from .bloomfilter import BloomFilter

DEFAULT_FPRATE = 1e-6
DEFAULT_NITEMS = int(1e9)
DEFAULT_NWORKERS = 8
DEFAULT_SAMQSIZE = 1000

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(os.environ.get('LOG_LEVEL', 'INFO'))
LOGGER.addHandler(logging.StreamHandler())

MSG_DUPFRAC = 'duplicate fraction: %0.4f'
MSG_NOHEADER = 'no header lines detected'
MSG_NALIGN = 'alignments seen: %s'
MSG_NDUP = 'duplicates marked: %s'
MSG_NQNAME = 'qnames seen: %s'
MSG_NUNIQUE = 'approximate n of stored items (templates + read ends):  %s'
MSG_QNAMEGRP = 'singleton %s: input does not appear to be qname grouped'
MSG_VERSION = 'streammd version %s'

SENTINEL = 'STOP'
# refID in SAM spec is int32 so first element is > any legal value.
UNMAPPED = (2**31, -1, '')


def samrecords(headerq, samq, nconsumers, batchsize=50, infd=0, outfd=1):
    """
    Read records from a qname-grouped SAM file input stream and enqueue them
    in batches.

    Header lines are written directly to the output stream and also to the
    header queue.

    Args:
        headerq: multiprocessing.Queue to put header.
        samq: multiprocessing.Queue to put SAM records.
        nconsumers: number of consumer processes.
        batchsize: number of lines per batch in samq (default=50).
        infd: input file descriptor (default=0).
        outfd: stream file descriptor (default=1).

    Returns:
        None
    """
    group = []
    groupid = None
    header = None
    headlines = []
    batch = []
    with open(infd) as infh:
        for line in infh:
            if line.startswith('@'):
                headlines.append(line)
                os.write(outfd, line.encode('ascii'))
            else:
                if not header:
                    if not headlines:
                        raise ValueError(MSG_NOHEADER)
                    header = ''.join(headlines)
                    for _ in range(nconsumers):
                        headerq.put(header)
                record = line.strip()
                qname = record.partition('\t')[0]
                if qname == groupid:
                    group.append(record)
                else:
                    if group:
                        if not len(group) > 1:
                            raise ValueError(MSG_QNAMEGRP % qname)
                        batch.append(group)
                        if len(batch) == batchsize:
                            samq.put(batch)
                            batch = []
                    groupid = qname
                    group = [record]
    batch.append(group)
    samq.put(batch)
    for _ in range(nconsumers):
        samq.put(SENTINEL)


def markdups(bfconfig, headerq, samq, outfd=1):
    """
    Process SAM file records.

    Args:
        bfconfig: Bloom filter configuration dict.
        headerq: multiprocessing.Queue to get header.
        samq: multiprocessing.Queue to get batches of paired SAM records.
        outfd: output stream file descriptor (default=1).

    Returns:
        (n_qname, n_dupe): number of qnames and number of duplicates seen.
    """
    bf = BloomFilter.copy(bfconfig)
    header = AlignmentHeader.from_text(headerq.get())
    n_qname, n_align, n_dup = 0, 0, 0
    while True:
        batch = samq.get()
        if batch == SENTINEL:
            break
        for group in batch:
            n_qname += 1
            n_align += len(group)
            alignments = [AlignedSegment.fromstring(r, header) for r in group]
            if not (ends := readends(alignments)):
                continue
            ends_str = [f'{end[0]}_{end[1]}{end[2]}' for end in ends]
            if ends[1] == UNMAPPED and not bf.add(ends_str[0]):
                # Replicate Picard MarkDuplicates behaviour: only the aligned
                # read is marked as duplicate.
                for a in alignments:
                    if a.is_mapped:
                        a.flag += 1024
                        n_dup += 1
            elif not bf.add(''.join(ends_str)):
                for a in alignments:
                    a.flag += 1024
                    n_dup += 1

            # Write the group as a group. In contrast to sys.stdout.write,
            # os.write is atomic so we don't have to care about locking or
            # using an output queue.
            out = '\n'.join(a.to_string() for a in alignments) + '\n'
            os.write(outfd, out.encode('ascii'))
    return (n_qname, n_align, n_dup)


def readends(alignments):
    """
    Calculate ends of the fragment, accounting for soft-clipped bases.

    Args:
        alignments: qname group tuple of AlignedSegment instances.

    Returns:
        None if there are no aligned reads, otherwise a coordinate-sorted pair
        of ends:

            [(left_refid, left_pos, left_orientation),
                (right_refid, right_pos, right_orientation)]

        a single unmapped end always appears last with the value UNMAPPED.
    """
    r12 = [None, None]
    ends = [UNMAPPED, UNMAPPED]

    # Pick the primary alignments.
    for alignment in alignments:
        if not (alignment.is_secondary or alignment.is_supplementary):
            if alignment.is_read1:
                r12[0] = alignment
            elif alignment.is_read2:
                r12[1] = alignment

    # Bail if neither aligns.
    if all(r.is_unmapped for r in r12):
        return None

    # Calculate the ends.
    for i, r in enumerate(r12):
        if r.is_unmapped:
            pass
        elif r.is_forward:
            # Leading soft clips.
            front_s = r.cigar[0][1] if r.cigar[0][0] == 4 else 0
            ends[i] = r.reference_id, r.reference_start - front_s, 'F'
        elif r.is_reverse:
            # Trailing soft clips.
            back_s = r.cigar[-1][1] if r.cigar[-1][0] == 4 else 0
            ends[i] = r.reference_id, r.reference_end + back_s, 'R'

    # Canonical ordering: l < r and UNMAPPED is always last by construction.
    ends.sort()
    return ends


def parse_cmdargs(args):
    """
    Returns: Parsed arguments
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',
                        default=0,
                        help=('Input SAM file. If not supplied, default is '
                              'STDIN.'))
    parser.add_argument('--output',
                        default=1,
                        help=('Output SAM file. If not supplied, default is '
                              'STDOUT.'))
    parser.add_argument('-n', '--n-items',
                        type=int,
                        default=DEFAULT_NITEMS,
                        help=('Expected maximum number of read pairs n '
                              f'(default={DEFAULT_NITEMS}).'))
    parser.add_argument('-p', '--fp-rate',
                        type=float,
                        default=DEFAULT_FPRATE,
                        help=('Target maximum false positive rate when n '
                              f'items are stored (default={DEFAULT_FPRATE}).'))
    parser.add_argument('--consumer-processes',
                        type=int,
                        default=DEFAULT_NWORKERS,
                        help=('Number of hashing processes '
                              f'(default={DEFAULT_NWORKERS}).'))
    parser.add_argument('--mem-calc',
                        type=float,
                        nargs=2,
                        metavar=('N_ITEMS', 'FP_RATE'),
                        help=('Print approximate memory requirement in GB '
                        'for n items and target maximum false positive rate '
                        'p.'))
    parser.add_argument('--queue-size',
                        type=int,
                        default=DEFAULT_SAMQSIZE,
                        help=('Size of the SAM record queue '
                              f'(default={DEFAULT_SAMQSIZE}).'))
    parser.add_argument('--version',
                        action='version',
                        version=metadata.version('streammd'))
    return parser.parse_args(args)


def mem_calc(n, p):
    """
    Returns approximate memory requirement in GB for n items and target maximum
    false positive rate p.
    """
    m, _ = BloomFilter.optimal_m_k(n, p)
    return m / 8 / 1024 ** 3


def main():
    """
    Run as CLI script
    """
    args = parse_cmdargs(sys.argv[1:])
    if args.mem_calc:
        print('%0.3fGB' % mem_calc(*args.mem_calc))
        sys.exit(0)
    LOGGER.info(MSG_VERSION, metadata.version('streammd'))
    LOGGER.info(' '.join(sys.argv))
    manager = Manager()
    headerq = manager.Queue(args.consumer_processes)
    samq = manager.Queue(args.queue_size)
    nconsumers = args.consumer_processes
    with open(args.input) as infh, open(args.output, 'wt') as outfh:
        infd, outfd = infh.fileno(), outfh.fileno()
        producer = Process(target=samrecords,
                        args=(headerq, samq, nconsumers),
                        kwargs={'infd':infd,
                                'outfd':outfd})
        producer.start()
        with SharedMemoryManager() as smm, Pool(nconsumers) as pool:
            bf = BloomFilter(smm, args.n_items, args.fp_rate)
            mdargs = repeat((bf.config, headerq, samq, outfd), nconsumers)
            counts = pool.starmap(markdups, mdargs)
            n_qname, n_align, n_dup = [sum(col) for col in zip(*counts)]
            n_unique = bf.count()
            producer.join()
    LOGGER.info(MSG_NUNIQUE, n_unique)
    LOGGER.info(MSG_NQNAME, n_qname)
    LOGGER.info(MSG_NALIGN, n_align)
    LOGGER.info(MSG_NDUP, n_dup)
    LOGGER.info(MSG_DUPFRAC, (n_dup/n_align))


if __name__ == '__main__':
    main()
