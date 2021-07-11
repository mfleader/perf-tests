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
]





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

  def _parse_raw(self):
    raw = self.out.split('seconds')
    raw = raw[1].split('[Status]')
    self.raw = [ 
      self._parse_raw_line(line) 
      for line in raw[0].split('\n') if len(line) > 0 
    ]

  def _parse_raw_line(self, line):
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







def main():
  with open('mocks/huge-raw.txt', 'r') as smallf:
    smalld = smallf.read()
    parser = Parser(smalld)
    parser.parse()


if __name__ == '__main__':
  main()