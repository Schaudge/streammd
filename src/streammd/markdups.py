"""
Mark duplicates on SAM file input stream.
"""
from multiprocessing import Queue, Process
from multiprocessing.managers import SharedMemoryManager
import os
from bloomfilter import BloomFilter
from pysam import AlignmentHeader, AlignedSegment

DEFAULT_WORKERS = 8
SENTINEL = 'STOP'


def samrecords(headerq, samq, nconsumers, batchsize=50, infd=0, outfd=1):
    """
    Read records from a SAM file input stream and enqueue them in batches.

    Header lines are written directly to the output stream and also to the
    header queue.

    Args:
        headerq: multiprocessing.Queue to put header.
        samq: multiprocessing.Queue to put SAM records.
        nconsumers: number of consumer processes.
        batchsize: number of lines per batch in samq (default=50).
        infd: input stream file descriptor (default=0).
        outfd: output stream file descriptor (default=1).

    Returns:
        None
    """
    samlines = []
    headlines = []
    header = None
    for line in os.fdopen(infd):
        if line.startswith('@'):
            headlines.append(line)
            # os.write is atomic (unlike sys.stdout.write)
            os.write(outfd, line.encode('ascii'))
        else:
            if not header:
                header = ''.join(headlines)
                for _ in range(nconsumers):
                    headerq.put(header)
            samlines.append(line.strip())
            if len(samlines) == batchsize:
                samq.put(samlines)
                samlines = []
    samq.put(samlines)
    for _ in range(nconsumers):
        samq.put(SENTINEL)


def markdups(headerq, samq, outfd=1):
    """
    Process SAM file records.

    Args:
        headerq: multiprocessing.Queue to get header.
        samq: multiprocessing.Queue to get batches of SAM records.
        outfd: output stream file descriptor (default=1).

    Returns:
        None
    """
    header = AlignmentHeader.from_text(headerq.get())
    while True:
        batch = samq.get()
        if batch == SENTINEL:
            break
        for line in batch:
            alignment = AlignedSegment.fromstring(line, header)
            os.write(outfd, (alignment.to_string()+'\n').encode('utf-8'))


def main():
    nconsumers = DEFAULT_WORKERS
    headerq = Queue(nconsumers)
    samq = Queue(1000)
    producer = Process(target=samrecords, args=(headerq, samq, nconsumers))
    producer.start()
    consumers = [
        Process(target=markdups, args=(headerq, samq))
        for _ in range(nconsumers)
    ]
    list(map(lambda x: x.start(), consumers))
    list(map(lambda x: x.join(), consumers))
    producer.join()


if __name__ == '__main__':
    main()

# def read_bam(
#def add_batch(bf_vars, bf_bits, items):
#    bf = BloomFilter.copy(bf_vars, bf_bits)
#    dupes = 0
#    for item in items:
#        present = bf.add(item)
#        if present:
#            dupes += 1
#    return dupes
#
#
#def main():
#    """
#    Test it
#    """
#    nconsumers = 10
#    with SharedMemoryManager() as smm,\
#            ProcessPoolExecutor(max_nconsumers=nconsumers) as ppe:
#
#        target_size = int(1e7)
#        bf = BloomFilter(smm, target_size, 1e-7)
#
#        # Here we assume we know the list of items in advance, so we can
#        # construct N chunks of items in advance. When we're reading through a
#        # bam in real time that won't be the case but we can do something like
#        # create 1 reader per contig, so 'items' is the list of contigs and
#        # the mapped func takes a contig name as an argument. TODO What to do
#        # for coordinate unsorted bams though?
#
#        # this creates 4 duplicates of every unique value
#        items = list(str(i) for i in range(int(target_size/5))) * 5
#        shuffle(items)
#
#
#        def chunker(l, n):
#            """
#            yield striped chunks
#            """
#            for i in range(0, n):
#                yield l[i::n]
#
#        chunks = chunker(items, nconsumers)
#
#        batch_args = ((bf.shl_vars.shm.name, bf.shm_bits.name, chunk)
#                      for chunk in chunks)
#        dupes = ppe.map(add_batch, *zip(*batch_args))
#        ppe.shutdown()
#
#        print(f'{len(items)} total items added (true)')
#        print(f'{bf.count()} unique items added (approx)')
#        print(f'{sum(dupes)} duplicates')
#        check = [0, 1, 10, 100, 1000, 10000, 100000, 1000000, 2000000, 10000000]
#        for j in check:
#            in_filt = str(j) in bf
#            print(f'{j} {"is" if in_filt else "is NOT"} present')
#    #for k in range(target_size, 2*target_size):
#    #    in_filt = str(k) in bf
#    #    if in_filt:
#    #        print(f'FP {k}')

