#!/usr/bin/env python3

# Copyright 2016 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import numpy
import re
import sqlite3

from params import PARAMETERS

_log = logging.getLogger(__name__)


class ResultDb(object):
  def __init__(self, dbfile):
    self.db = sqlite3.connect(dbfile)

    self.c = self.db.cursor()

    sql = """-- run parameters
CREATE TABLE IF NOT EXISTS runs (
  run_id,
  run_subid,
  pod_name,
  {params},
  primary key (run_id, run_subid, pod_name)
)""".format(params=',\n  '.join([param.name for param in PARAMETERS]))
    self.c.execute(sql)
    _log.debug('%s', sql)

    sql = """-- run results
CREATE TABLE IF NOT EXISTS results (
  run_id,
  run_subid,
  pod_name,
  {results},
  primary key (run_id, run_subid, pod_name)
)""".format(results=',\n  '.join([r.name for r in RESULTS]))
    _log.debug('%s', sql)
    self.c.execute(sql)

    sql = """-- latency histogram
CREATE TABLE IF NOT EXISTS histograms (
  run_id,
  run_subid,
  pod_name,
  rtt_ms,
  rtt_ms_count
)
"""
    _log.debug('%s', sql)
    self.c.execute(sql)

    _log.info('Using DB %s', dbfile)

  def put(self, results, ignore_if_dup=True):
    key = [results['params']['run_id'], results['params']['run_subid'], results['params']['pod_name']]
    if self._exists(key) and ignore_if_dup:
      _log.info('Ignoring duplicate results %s', key)
      return

    sql = ('INSERT INTO runs (run_id, run_subid, pod_name,'
           + ','.join([p.name for p in PARAMETERS])
           + ') VALUES ('
           + ','.join(['?'] * (3 + len(PARAMETERS)))
           + ')')
    _log.debug('runs sql -- %s', sql)
    self.c.execute(sql, key + [
        results['params'][p.name] if p.name in results['params'] else None
        for p in PARAMETERS
    ])

    sql = ('INSERT INTO results (run_id, run_subid, pod_name,'
           + ','.join([r.name for r in RESULTS])
           + ') VALUES ('
           + ','.join(['?'] * (3 + len(RESULTS)))
           + ')')
    _log.debug('results sql -- %s', sql)
    self.c.execute(sql, key +
                   [results['data'][r.name]
                       if r.name in results['data'] else None
                    for r in RESULTS])

    for rtt_ms, count in results['data']['histogram']:
      data = {
          'run_id': results['params']['run_id'],
          'run_subid': results['params']['run_subid'],
          'rtt_ms': rtt_ms,
          'rtt_ms_count': count,
      }

      columns = ','.join(list(data.keys()))
      qs = ','.join(['?'] * len(data))
      stmt = 'INSERT INTO histograms (' + columns + ') VALUES (' + qs + ')'
      _log.debug('histogram sql -- %s', stmt)
      self.c.execute(stmt, list(data.values()))

  def get_results(self, run_id, run_subid):
    sql = ('SELECT ' + ','.join([r.name for r in RESULTS])
           + ' FROM results WHERE run_id = ? and run_subid = ? and pod_name = ?')
    _log.debug('%s', sql)
    self.c.execute(sql, (run_id, run_subid, pod_name))
    rows = self.c.fetchall()
    return dict(list(zip([r.name for r in RESULTS], rows[0]))) if rows else None

  def _exists(self, key):
    self.c.execute(
        'SELECT COUNT(*) FROM runs WHERE run_id = ? and run_subid = ? and pod_name = ?', key)
    count = self.c.fetchall()[0][0]
    return count != 0

  def commit(self):
    self.db.commit()


class Result(object):
  """
  Represents a column in the results table.
  """
  def __init__(self, name, val_type, regex):
    self.name = name
    self.val_type = val_type
    self.regex = regex

RESULTS = [
    Result('queries_sent', int,
           re.compile(r'\s*Queries sent:\s*(\d+)')),
    Result('queries_completed', int,
           re.compile(r'\s*Queries completed:\s*(\d+).*')),
    Result('queries_lost', int,
           re.compile(r'\s*Queries lost:\s*(\d+).*')),
    Result('run_time', float,
           re.compile(r'\s*Run time \(s\):\s*([0-9.]+)')),
    Result('qps', float,
           re.compile(r'\s*Queries per second:\s*([0-9.]+)')),

    Result('avg_latency', float,
           re.compile(r'\s*Average Latency \(s\):\s*([0-9.]+).*')),
    Result('min_latency', float,
           re.compile(r'\s*Average Latency \(s\):.*min ([0-9.]+).*')),
    Result('max_latency', float,
           re.compile(r'\s*Average Latency \(s\):.*max ([0-9.]+).*')),
    Result('stddev_latency', float,
           re.compile(r'\s*Latency StdDev \(s\):\s*([0-9.]+)')),
    Result('max_perfserver_cpu', int, None),
    Result('max_perfserver_memory', int, None),
    Result('max_kubedns_cpu', int, None),
    Result('max_kubedns_memory', int, None),
    # Derived results
    Result('latency_50_percentile', float, None),
    Result('latency_95_percentile', float, None),
    Result('latency_99_percentile', float, None),
    Result('latency_99_5_percentile', float, None),
]


import attr

@attr.s(slots=True)
class DnsperfQueryData:
  rcode = attr.ib()
  fqdn = attr.ib()
  qtype = attr.ib()
  rtt_mu_s = attr.ib()


class Parser(object):
  """
  Parses dnsperf output file.
  """
  def __init__(self, out):
    self.out = out
    self.lines = [x.strip() for x in out.split('\n')]
    # print('------------------lines--------------')
    # print(self.lines)
    self.results = {}
    self.histogram = []
    self.raw = []

  def parse(self):
    self._parse_raw()
    self._parse_results()
    # self._parse_histogram()
    # self._compute_derived()

  def _parse_raw(self):
    raw = self.out.split('seconds')
    raw = raw[1].split('[Status]')
    self.raw = [ 
      self._parse_raw_line(line) 
      for line in raw[0].split('\n') if len(line) > 0 
    ]

  def _parse_raw_line(self, line) -> DnsperfQueryData:
    words = line.split(' ')
    # return DnsperfQueryData(
    #   rcode = words[1],
    #   fqdn = words[2],
    #   qtype = words[3],
    #   rtt_mu_s = int(float(words[4]) * 10**6)
    # )
    # convert seconds to microseconds
    return [words[1], words[2], words[3], int(float(words[4]) * 10**6)]

  def _parse_results(self):
    results = {}
    for line in self.lines:
      for result in RESULTS:
        if result.regex is None:
          continue
        match = result.regex.match(line)
        if not match:
          continue
        results[result.name] = result.val_type(match.group(1))
    self.results = results
    # print('-------------results------------')
    # print(results)

  def _parse_histogram(self):
    lines = [x for x in self.lines if re.match('^#histogram .*', x)]
    for line in lines:
      match = re.match(r'^#histogram\s+(\d+) (\d+)', line)
      rtt, count = [int(x) for x in match.groups()]
      self.histogram.append([rtt, count])

  def _compute_derived(self):
    # Note: not very efficient, but functional
    from functools import reduce
    histogram = reduce(
        list.__add__,
        [[rtt]*count for rtt, count in self.histogram],
        [])
    _log.debug('Latency histogram = %s', histogram)

    for name, ptile in [('latency_50_percentile', 50),
                        ('latency_95_percentile', 95),
                        ('latency_99_percentile', 99),
                        ('latency_99_5_percentile', 99.5)]:
      self.results[name] = float(numpy.percentile(histogram, ptile)) # pylint: disable=no-member





def main():
  with open('mocks/huge-raw.txt', 'r') as smallf:
    smalld = smallf.read()
    parser = Parser(smalld)
    parser.parse()


if __name__ == '__main__':
  main()