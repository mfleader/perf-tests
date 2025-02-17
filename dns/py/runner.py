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
import copy
import logging
import os
import subprocess
import time
import traceback
import threading
import re
import queue
from subprocess import PIPE

import orjson
import yaml

from data import Parser
from params import ATTRIBUTE_CLUSTER_DNS, ATTRIBUTE_NODELOCAL_DNS, Inputs, TestCases, QueryFile, RunLengthSeconds

_log = logging.getLogger(__name__)
_app_label = 'app=dns-perf-server'
_client_label = 'app=dns-perf-client'
_test_svc_label = 'app=test-svc'
_dnsperf_qfile_name='queryfile-example-current'
_dnsperf_qfile_path='ftp://ftp.nominum.com/pub/nominum/dnsperf/data/queryfile-example-current.gz'
# Remove dns queries to this host since it is associated with behavior pattern
# of some malware
_remove_query_pattern=["setting3[.]yeahost[.]com"]
MAX_TEST_SVC = 20

def add_prefix(prefix, text):
  decoded_text = isinstance(text, str) and str or text.decode()
  return '\n'.join([prefix + l for l in decoded_text.split('\n')])


class Runner(object):
  """
  Runs the performance experiments.
  """
  def __init__(self, args):
    """
    |args| parsed command line args.
    """
    self.args = args
    self.deployment_yaml = yaml.safe_load(open(self.args.deployment_yaml, 'r'))
    self.configmap_yaml = yaml.safe_load(open(self.args.configmap_yaml, 'r')) if \
      self.args.configmap_yaml else None
    self.service_yaml = yaml.safe_load(open(self.args.service_yaml, 'r')) if \
        self.args.service_yaml else None
    self.dnsperf_yaml = yaml.safe_load(open(self.args.dnsperf_yaml, 'r'))
    self.test_params = TestCases.load_from_file(args.params)
    if self.args.run_large_queries:
      self.test_params.set_param(QueryFile().name, _dnsperf_qfile_name)
    self.args.testsvc_yaml = yaml.safe_load(open(self.args.testsvc_yaml, 'r')) if \
        self.args.testsvc_yaml else None
    self.server_node = None
    self.use_existing = False
    self.attributes = set()

    if self.args.use_cluster_dns:
      _log.info('Using cluster DNS for tests')
      self.args.dns_ip = self._get_dns_ip(self.args.dns_server)
      self.attributes.add(ATTRIBUTE_CLUSTER_DNS)
      self.use_existing = True
    elif self.args.nodecache_ip:
      _log.info('Using existing node-local-dns for tests')
      self.args.dns_ip = self.args.nodecache_ip
      self.attributes.add(ATTRIBUTE_NODELOCAL_DNS)
      self.use_existing = True

    _log.info('DNS service IP is %s', args.dns_ip)

  def go(self):
    """
    Run the performance tests.
    """
    self._select_nodes()

    test_cases = self.test_params.generate(self.attributes)
    if len(test_cases) == 0:
      _log.warning('No test cases')
      return 0

    try:
      self._ensure_out_dir(test_cases[0].run_id)
      client_pods=self._reset_client()
      _log.info('Starting creation of test services')
      self._create_test_services()

      last_deploy_yaml = None
      last_config_yaml = None
      for test_case in test_cases:
        try:
          print(f"test case: {test_case}")
          inputs = Inputs(self.deployment_yaml, self.configmap_yaml,
                          ['/dnsperf', '-s', self.args.dns_ip])
          # print(f'{inputs}')  

          test_case.configure(inputs)
          # pin server to a specific node

          inputs.deployment_yaml['spec']['template']['spec']['nodeName'] = \
              self.server_node
          
          if not self.use_existing and (
              yaml.dump(inputs.deployment_yaml) !=
              yaml.dump(last_deploy_yaml) or
              yaml.dump(inputs.configmap_yaml) !=
              yaml.dump(last_config_yaml)):
            _log.info('Creating server with new parameters')
            self._teardown()
            self._create(inputs.deployment_yaml)
            self._create(self.service_yaml)
            if self.configmap_yaml is not None:
              self._create(self.configmap_yaml)
            self._wait_for_status(True)
          test_threads=[]
          #Spawn off a thread to run the test case in each client pod simultaneously.
          for podname in client_pods:
            _log.debug('Running test in pod %s', podname)
            tc = copy.copy(test_case)
            tc.pod_name = podname
            dt = threading.Thread(target=self._run_perf,args=[tc,inputs, podname])
            test_threads.append(dt)
            dt.start()
          for thread in test_threads:
            thread.join()
          last_deploy_yaml = inputs.deployment_yaml
          last_config_yaml = inputs.configmap_yaml

        except Exception:
          _log.info('Exception caught during run, cleaning up. %s',
                    traceback.format_exc())
          # print(f"except inputs: {inputs}")                    
          self._teardown()
          self._teardown_client()
          raise

    finally:
      self._teardown()
      self._teardown_client()

    return 0

  def _run_perf(self, test_case, inputs, podname):
    _log.info(f'Running test case: {test_case}')
    output_file = (
        f"{self.args.out_dir}/"
        f"run-{test_case.run_id}/"
        f"result-{test_case.run_subid}-{test_case.pod_name}"
    )
    header = (
        f"### run_id {test_case.run_id}:{test_case.run_subid}\n"
        f"### date {time.ctime()}\n"
        f"### settings {orjson.dumps(test_case.to_yaml())}"
    )           
    _log.info(f'Writing to output file {output_file}')

    with open(output_file + '.raw', 'w') as fh:
      fh.write(header)
      cmdline = inputs.dnsperf_cmdline
      cmdline = [*cmdline, '-v']
      # print(f'** dnsperf cmdline: {cmdline}')
      code, out, err = self._kubectl(
          *([None, 'exec', podname, '--'] + [str(x) for x in cmdline]))
      fh.write(f"{add_prefix('out | ', out)}\n")
      fh.write(f"{add_prefix('err | ', err)}\n")

      if code != 0:
        raise Exception(f'error running dnsperf - {err}, podname {podname}')

    with open(output_file, 'wb') as fh:
      results = {}
      results['metadata'] = test_case.to_yaml()
      results['metadata']['datetime'] = time.ctime()
      results['code'] = code
      results['data'] = {}

      try:
        parser = Parser(out)
        parser.parse()
        _log.info('Test results parsed')
        results['data']['ok'] = True
        results['data']['msg'] = None
        for key, value in list(parser.results.items()):
          results['data'][key] = value
        results['data']['raw'] = parser.raw

      except Exception:
        _log.exception('Error parsing results.')
        results['data']['ok'] = False
        results['data']['msg'] = f'parsing error:\n{traceback.format_exc()}'

      fh.write(orjson.dumps(results))    

  def _kubectl(self, stdin, *args):
    """
    |return| (return_code, stdout, stderr)
    """
    cmdline = [self.args.kubectl_exec] + list(args)
    _log.debug('kubectl %s', cmdline)
    proc = subprocess.Popen(cmdline, stdin=PIPE, stdout=PIPE, stderr=PIPE)
    if isinstance(stdin, str):
      stdin = stdin.encode()
    out, err = proc.communicate(stdin)
    ret = proc.wait()

    _log.debug('kubectl ret=%d', ret)
    _log.debug('kubectl stdout\n%s', add_prefix('out | ', out))
    _log.debug('kubectl stderr\n%s', add_prefix('err | ', err))

    return proc.wait(), out.decode(), err.decode()

  def _create(self, yaml_obj):
    _log.debug('applying yaml: %s', yaml.dump(yaml_obj))
    ret, out, err = self._kubectl(yaml.dump(yaml_obj, encoding='utf-8'), 'create', '-f', '-')
    if ret != 0:
      _log.error('Could not create dns: %d\nstdout:\n%s\nstderr:%s\n',
                 ret, out, err)
      raise Exception('create failed')
    _log.info('Create %s/%s ok', yaml_obj['kind'], yaml_obj['metadata']['name'])


  def _create_test_services(self):
    if not self.args.testsvc_yaml:
      _log.info("Not creating test services since no yaml was provided")
      return
    # delete existing services if any
    self._kubectl(None, 'delete', 'services', '-l', _test_svc_label)

    for index in range(1,MAX_TEST_SVC + 1):
      self.args.testsvc_yaml['metadata']['name'] = "test-svc" + str(index)
      self._create(self.args.testsvc_yaml)

  def _select_nodes(self):
    code, out, _ = self._kubectl(None, 'get', 'nodes', '-o', 'yaml')
    if code != 0:
      raise Exception('error gettings nodes: %d', code)

    nodes = [n['metadata']['name'] for n in yaml.safe_load(out)['items']
             if not ('unschedulable' in n['spec'] \
                 and n['spec']['unschedulable'])]
    if len(nodes) < 2 and not self.args.single_node:
      raise Exception('you need 2 or more worker nodes to run the perf test')

    if self.use_existing:
      return

    if self.args.server_node:
      if self.args.server_node not in nodes:
        raise Exception('%s is not a valid node' % self.args.server_node)
      _log.info('Manually selected server_node')
      self.server_node = self.args.server_node
    else:
      self.server_node = nodes[0]

    _log.info('Server node is %s', self.server_node)

  def _get_dns_ip(self, svcname):
    code, out, _ = self._kubectl(None, 'get', 'svc', '-o', 'yaml',
                                svcname, '-nopenshift-dns')

    if code != 0:
      raise Exception('error gettings dns ip for service %s: %d' %(svcname, code))

    try:
      return yaml.safe_load(out)['spec']['clusterIP']
    except:
      raise Exception('error parsing %s service, could not get dns ip' %(svcname))

  def _teardown(self):
    _log.info('Starting server teardown')

    self._kubectl(None, 'delete', 'deployments', '-l', _app_label)
    self._kubectl(None, 'delete', 'services', '-l', _app_label)
    self._kubectl(None, 'delete', 'configmap', '-l', _app_label)

    self._wait_for_status(False)

    _log.info('Server teardown ok')

    self._kubectl(None, 'delete', 'services', '-l', _test_svc_label)
    if self.args.run_large_queries:
      try:
        subprocess.check_call(['rm', self.args.query_dir +_dnsperf_qfile_name])
      except subprocess.CalledProcessError:
        _log.info("Failed to delete query file")

  def _reset_client(self):
    self._teardown_client()

    self._create(self.dnsperf_yaml)
    client_pods=[]
    while True:
      code, pending, err = self._kubectl(None, 'get','deploy', 'dns-perf-client','--no-headers', '-o', 'custom-columns=:status.unavailableReplicas')
      if pending.rstrip() != "<none>":
        #Deployment not ready yet
        _log.info("pending replicas in client deployment - '%s'", pending.rstrip())
        time.sleep(5)
        continue
      client_pods = self._get_pod(_client_label)
      if len(client_pods) > 0:
        break
      _log.debug('waiting for client pods')

    for podname in client_pods:
      while True:
        code, _, _ = self._kubectl(
            None, 'exec', '-i', podname, '--', 'echo')
        if code == 0:
          break
        time.sleep(1)
      _log.info('Client pod ready for execution')
      self._copy_query_files(podname)
    return client_pods

  def _copy_query_files(self, podname):
    if self.args.run_large_queries:
      try:
        _log.info('Downloading large query file')
        subprocess.check_call(['wget', _dnsperf_qfile_path])
        subprocess.check_call(['gunzip', _dnsperf_qfile_path.decode().split('/')[-1]])
        _log.info('Removing hostnames matching specified patterns')
        for pattern in _remove_query_pattern:
          subprocess.check_call(['sed', '-i', '-e', '/%s/d' %(pattern), _dnsperf_qfile_name])
        subprocess.check_call(['mv', _dnsperf_qfile_name, self.args.query_dir])

      except subprocess.CalledProcessError:
          _log.info('Exception caught when downloading query files %s',
                    traceback.format_exc())

    _log.info('Copying query files to client %s', podname)
    tarfile_contents = subprocess.check_output(
        ['tar', '-czf', '-', self.args.query_dir])
    code, _, _ = self._kubectl(
        tarfile_contents,
        'exec', '-i', podname, '--', 'tar', '-xzf', '-')
    if code != 0:
      raise Exception('error copying query files to client: %d' % code)
    _log.info('Query files copied')

  def _teardown_client(self):
    _log.info('Starting client teardown')
    self._kubectl(None, 'delete', 'deployment/dns-perf-client')

    while True:
      client_pods = self._get_pod(_client_label)
      if len(client_pods) == 0:
        break
      time.sleep(1)
      _log.info('Waiting for client pod to terminate')

    _log.info('Client teardown complete')

  def _wait_for_status(self, active):
    while True:
      code, out, err = self._kubectl(
          None, 'get', '-o', 'yaml', 'pods', '-l', _app_label)
      if code != 0:
        _log.error('Error: stderr\n%s', add_prefix('err | ', err))
        raise Exception('error getting pod information: %d', code)
      pods = yaml.safe_load(out)

      _log.info('Waiting for server to be %s (%d pods active)',
                'up' if active else 'deleted',
                len(pods['items']))

      if (active and len(pods['items']) > 0) or \
         (not active and len(pods['items']) == 0):
        break

      time.sleep(1)

    if active:
      break_loop = False
      while True:
        client_pods = self._get_pod(_client_label)
        if len(client_pods) == 0:
          continue
        for podname in client_pods:
          code, out, err = self._kubectl(
            None,
            'exec', podname, '--',
            'dig', '@' + self.args.dns_ip,
            'kubernetes.default.svc.cluster.local.')

          if code == 0:
            break_loop = True
            break
        if break_loop:
          break

        _log.info('Waiting for DNS service to start')

        time.sleep(1)

      _log.info('DNS is up')

  def _get_pod(self, label, output='custom-columns=:metadata.name'):
    code, client_pods, err = self._kubectl(None, 'get', 'pods', '-l', label, '--no-headers', '-o',
                                           output)
    if code != 0:
      _log.error('Error: stderr\n%s', add_prefix('err | ', err))
      return []
    client_pods = client_pods.rstrip().split('\n') if len(client_pods) != 0 else []
    _log.info('got client pods "%s"', client_pods)
    return client_pods

  def _ensure_out_dir(self, run_id):
    rundir_name = 'run-%s' % run_id
    rundir = os.path.join(self.args.out_dir, rundir_name)
    latest = os.path.join(self.args.out_dir, 'latest')

    if not os.path.exists(rundir):
      os.makedirs(rundir)
      _log.info('Created rundir %s', rundir)

    if os.path.exists(latest):
      os.unlink(latest)
    os.symlink(rundir_name, latest)

    _log.info('Updated symlink %s', latest)
