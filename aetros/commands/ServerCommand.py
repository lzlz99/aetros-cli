from __future__ import absolute_import
from __future__ import print_function
import argparse
import os
import sys
import time
import psutil
import subprocess

from aetros.backend import EventListener, BackendClient
from aetros.utils import read_home_config


class ServerClient(BackendClient):
    def __init__(self, host, event_listener, logger):
        BackendClient.__init__(self, host, event_listener, logger)
        self.server_name = None

    def configure(self, server_name):
        self.server_name = server_name

    def on_connect(self):
        self.send_message({'type': 'register_server', 'server': self.server_name})
        messages = self.wait_for_at_least_one_message()

        if not messages:
            return False

        message = messages.pop(0)
        if isinstance(message, dict) and 'a' in message:
            if "registration_failed" in message['a']:
                self.logger.error('Access denied. Try a different secure key.')
                self.close()
                self.event_listener.fire('failed')
                return False

            if "already_registered" in message['a']:
                self.logger.error("Registration failed. This server is already registered.")
                self.close()
                self.event_listener.fire('failed')
                return False

            if "registered" in message['a']:
                self.registered = True
                self.event_listener.fire('registration')
                self.logger.info("Connected to %s as server %s" % (self.host, self.server_name))
                self.handle_messages(messages)
                return True

        self.logger.error("Registration of server %s failed due to protocol error." % (self.server_name,))
        return False

    def handle_messages(self, messages):
        for message in messages:
            if not isinstance(message, dict):
                return

            if 'stop' in message:
                self.close()
                self.event_listener.fire('stop')

            if 'type' in message:
                if message['type'] == 'start-jobs':
                    self.event_listener.fire('start-jobs', message['jobs'])

                if message['type'] == 'stop-job':
                    self.event_listener.fire('stop-job', message['id'])


class ServerCommand:
    model = None
    job_model = None

    def __init__(self, logger):
        self.logger = logger
        self.last_utilization = None
        self.last_net = {}
        self.nets = []
        self.server = None
        self.active = True
        self.queue = []
        self.queuedMap = {}
        self.job_processes = []
        self.max_parallel_jobs = 2
        self.registered = False
        self.show_stdout = False

    def main(self, args):
        import aetros.const

        parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter,
                                         prog=aetros.const.__prog__ + ' server')
        parser.add_argument('name', nargs='?', help="Server name")
        parser.add_argument('--max-jobs', help="How many jobs should be run at the same time.")
        parser.add_argument('--host', help="Default trainer.aetros.com. Read from environment variable API_HOST.")
        parser.add_argument('--port', help="Default 8051. Read from environment variable API_PORT.")
        parser.add_argument('--show-stdout', action='store_true', help="Show all stdout of all jobs")

        parsed_args = parser.parse_args(args)

        if not parsed_args.name:
            parser.print_help()
            sys.exit()

        config = read_home_config()

        if parsed_args.max_jobs:
            self.max_parallel_jobs = int(parsed_args.max_jobs)
        if parsed_args.show_stdout:
            self.show_stdout = True

        event_listener = EventListener()

        event_listener.on('registration', self.registration_complete)
        event_listener.on('failed', self.connection_failed)
        event_listener.on('start-jobs', self.start_jobs)
        event_listener.on('stop-job', self.stop_job)

        self.server = ServerClient(parsed_args.host or config['host'], event_listener, self.logger)
        self.server.configure(parsed_args.name)
        self.server.start()

        while self.active:
            if self.registered:
                self.server.send_message({'type': 'utilization', 'values': self.collect_system_utilization()})
                self.process_queue()

            time.sleep(1)

    def connection_failed(self, params):
        self.active = False
        sys.exit(1)

    def start_jobs(self, jobs):
        for job in jobs:
            self.start_job(job)

    def stop_job(self, id):
        if id in self.queuedMap:
            job = self.queuedMap[id]
            self.logger.info("Queued job removed %s#%d (%s) " % (job['modelId'], job['index'], job['id']))

            # removing from the queue is enough, since the job process itself terminates it when job is aborted.
            if job in self.queue:
                self.queue.remove(job)

            del self.queuedMap[id]

    def start_job(self, job):
        self.check_finished_jobs()

        if job['id'] in self.queuedMap:
            return

        self.logger.info("Queued job %s#%d (%s, prio:%d) by %s in %s ..." % (
            job['modelId'], job['index'], job['id'], job['priority'], job['username'], os.getcwd()
        ))

        self.server.send_message({'type': 'job-queued', 'id': job['id']})

        self.queuedMap[job['id']] = job
        self.queue.append(job)

    def check_finished_jobs(self):
        for process in self.job_processes:
            job = getattr(process, 'job')
            exit_code = process.poll()
            if exit_code is not None and exit_code > 0:
                reason = 'Failed job %s. Exit status: %s' % (job['id'], str(exit_code))
                self.logger.error(reason)
                self.server.send_message({'type': 'job-failed', 'id': job['id'], 'error': reason})
            elif exit_code is not None and exit_code == 0:
                self.logger.info('Finished job %s. Exit status: %s' % (job['id'], str(exit_code)))

            if exit_code is not None and job['id'] in self.queuedMap:
                del self.queuedMap[job['id']]

        # remove dead job processes
        self.job_processes = [x for x in self.job_processes if x.poll() is None]

    def process_queue(self):
        self.check_finished_jobs()

        if len(self.job_processes) >= self.max_parallel_jobs:
            return

        if len(self.queue) > 0:
            # registered and free space for new jobs, so execute another one
            self.execute_job(self.queue.pop(0))

    def execute_job(self, job):
        self.logger.info("Execute job %s#%d (%s) by %s using job's API_KEY=%s in %s ..." % (
            job['modelId'], job['index'], job['id'], job['username'], job['apiKey'], os.getcwd()))

        with open(os.devnull, 'r+b', 0) as DEVNULL:
            my_env = os.environ.copy()

            if 'PYTHONPATH' not in my_env:
                my_env['PYTHONPATH'] = ''

            my_env['PYTHONPATH'] += ':' + os.getcwd()
            args = [sys.executable, '-m', 'aetros', 'start', job['id'], '--api-key=' + job['apiKey']]
            process = subprocess.Popen(args, stdin=DEVNULL, stdout=DEVNULL if not self.show_stdout else sys.stdout, stderr=sys.stderr, close_fds=True,
                                       env=my_env)
            setattr(process, 'job', job)
            self.job_processes.append(process)

    def registration_complete(self, params):
        self.registered = True

        # upon registration, we need to clear the queue, since the server sends us immediately
        # all to be enqueued jobs after registration/re-connection
        self.queue = []
        self.queueMap = {}

        self.server.send_message({'type': 'system', 'values': self.collect_system_information()})

    def collect_system_information(self):
        values = {}
        mem = psutil.virtual_memory()
        values['memory_total'] = mem.total

        import cpuinfo
        cpu = cpuinfo.get_cpu_info()
        values['cpu_name'] = cpu['brand']
        values['cpu'] = [cpu['hz_actual_raw'][0], cpu['count']]
        values['nets'] = {}
        values['disks'] = {}
        values['boot_time'] = psutil.boot_time()

        for disk in psutil.disk_partitions():
            try:
                name = self.get_disk_name(disk[1])
                values['disks'][name] = psutil.disk_usage(disk[1]).total
            except:
                pass

        for id, net in psutil.net_if_stats().items():
            if 0 != id.find('lo') and net.isup:
                self.nets.append(id)
                values['nets'][id] = net.speed or 1000

        return values

    def get_disk_name(self, name):

        if 0 == name.find("/Volumes"):
            return os.path.basename(name)

        return name

    def collect_system_utilization(self):
        values = {}

        values['cpu'] = psutil.cpu_percent(interval=0.2, percpu=True)
        mem = psutil.virtual_memory()
        values['memory'] = mem.percent
        values['disks'] = {}
        values['jobs'] = {'parallel': self.max_parallel_jobs, 'enqueued': len(self.queue), 'running': len(self.job_processes)}
        values['nets'] = {}
        values['processes'] = []

        for disk in psutil.disk_partitions():
            try:
                name = self.get_disk_name(disk[1])
                values['disks'][name] = psutil.disk_usage(disk[1]).used
            except:
                pass

        net_stats = psutil.net_io_counters(pernic=True)
        for id in self.nets:
            net = net_stats[id]
            values['nets'][id] = {
                'recv': net.bytes_recv,
                'sent': net.bytes_sent,
                'upload': 0,
                'download': 0
            }

            if id in self.last_net and self.last_utilization:
                values['nets'][id]['upload'] = (net.bytes_sent - self.last_net[id]['sent']) / (
                    time.time() - self.last_utilization)
                values['nets'][id]['download'] = (net.bytes_recv - self.last_net[id]['recv']) / (
                    time.time() - self.last_utilization)

            self.last_net[id] = dict(values['nets'][id])

        for p in psutil.process_iter():
            try:
                cpu = p.cpu_percent()
                if cpu > 1 or p.memory_percent() > 1:
                    values['processes'].append([
                        p.pid,
                        p.name(),
                        p.username(),
                        p.create_time(),
                        p.status(),
                        p.num_threads(),
                        p.memory_percent(),
                        cpu
                    ])
            except OSError:
                pass
            except psutil.Error:
                pass

        try:
            if hasattr(os, 'getloadavg'):
                values['loadavg'] = os.getloadavg()
            else:
                values['loadavg'] = ''
        except OSError:
            pass

        self.last_utilization = time.time()
        return values
