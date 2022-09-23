# streammd

Single-pass probabilistic duplicate marking of alignments with a Bloom filter.

Input is a SAM file format input stream: a valid header followed by reads in
qname-grouped order (e.g. output of `bwa mem`). Detected duplicates have the
SAM FLAG 0x400 bit set in the outputs and summary metrics are written to a file
at the end of processing.

### Features

* Fast — with default settings `streammd` is ~ 3x faster than Picard
  MarkDuplicates, and faster is easily achievable using more workers.
* Low memory use even for large libraries — with default settings `streammd`
  requires just 4G to process 1B templates.
* High concordance with Picard MarkDuplicates metrics.
* Soft-clipped reads are correctly handled.
* Tunable target false positive rate.
* Streaming input and output.

### Limitations

Inherent, due to the nature of the single-pass operation:

* `streammd` retains the first encountered template as the original and marks
  subsequently encountered copies as duplicates. This differs from Picard
  MarkDuplicates which marks those of lowest quality as the duplicates.
* `streammd` does not differentiate between optical duplicates and PCR
  duplicates.

Implementation specific:

* Output is not deterministic when using more than 1 worker process.

## Install


```bash
git clone https://github.com/delocalizer/streammd
cd streammd
./configure && make
make install
```

## Test

(optional, requires tox)
```bash
tox
```

## Usage

0. get help

```bash
streammd --help
```

1. mark duplicates on an input SAM file record stream 

```bash
samtools view -h some.bam|streammd
```

## Notes

### Memory usage

Minimum memory usage depends on the number of items to be stored `n` and target
maximum false positive rate `p`:

|    n     |    p     |   mem   |
| -------- | -------- | ------- |
| 1.00E+07 | 1.00E-02 | 0.01 GB |
| 1.00E+07 | 1.00E-04 | 0.02 GB |
| 1.00E+07 | 1.00E-06 | 0.04 GB |
| 1.00E+08 | 1.00E-02 | 0.12 GB |
| 1.00E+08 | 1.00E-04 | 0.24 GB |
| 1.00E+08 | 1.00E-06 | 0.36 GB |
| 1.00E+09 | 1.00E-02 | 1.20 GB |
| 1.00E+09 | 1.00E-04 | 2.40 GB |
| 1.00E+09 | 1.00E-06 | 3.59 GB |


As a guide, 60x human WGS 2x150bp paired-end sequencing consists of n &#8776;
6.00E+08 templates. Run the included `memcalc` tool to get an estimate of
minimum `streammd` memory use for a given `(n, p)`.

### Memory and performance

Bloom filter performance is strongly determined by the number of required hash
functions `k`. Since `k` is very sensitive to memory around the minimum `m`
value, allowing even slightly more than the minimum memory required for a given
`(n, p)` is always a good idea if you can afford it. As a rule of thumb,
allowing 1.25x the minimum halves the value of `k`. Run the included `memcalc`
tool to see the details of how `k` will vary with memory allowance.

|    n     |   p      |   mem    |  k           | 
| -------- | -------- | -------- | ------------ |
| 1.00E+09 | 1.00E-06 | 3.50  GB | no solution  |
| 1.00E+09 | 1.00E-06 | 3.59  GB | 20           | 
| 1.00E+09 | 1.00E-06 | 4.00  GB | 12           | 
| 1.00E+09 | 1.00E-06 | 4.00 GiB | 11           | 
| 1.00E+09 | 1.00E-06 | 4.49  GB | 10           | 
| 1.00E+09 | 1.00E-06 | 6.00  GB |  7           | 
| 1.00E+09 | 1.00E-06 | 8.00  GB |  6           | 

As a micro-optimization, note also that when mem is a power of two e.g. 4GiB
(not 4GB), a slightly faster method is used to map hashes into the Bloom filter
array. Thus exact values of 512MiB, 1GiB, 2GiB, 4GiB etc. give slightly better
performance than nearby values.

### Pipelining

By using multiple worker processes `streammd` is capable of generating outputs
at a very high rate. For efficient pipelining, downstream tools should be run
with sufficient cpu resources to handle their inputs — for example if you want
to write the outputs to bam format using `samtools view` you should specify
extra compression threads for optimal throughput:

```bash
samtools view -h some.bam|streammd|samtools view -@2 -o some.MD.bam
```

By the same token, there's no value in increasing `streammd` throughput with
larger values for `-w, --workers` if a downstream tool does not have sufficient
processing velocity.
