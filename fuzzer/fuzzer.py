import os
import time
import angr
import tempfile
import subprocess

import logging

l = logging.getLogger("fuzzer.fuzzer")

config = { }

class EarlyCrash(Exception):
    pass

class Fuzzer(object):
    ''' Fuzzer object, spins up a fuzzing job on a binary '''

    def __init__(self, binary_path, work_dir, afl_count=1, extra_opts=None, create_dictionary=False, seeds=None):
        '''
        :param binary_path: path to the binary to fuzz
        :param work_dir: the work directory which contains fuzzing jobs, our job directory will go here
        :param afl_count: number of AFL jobs total to spin up for the binary
        :param seeds: list of inputs to seed fuzzing with
        :param extra_opts: extra options to pass to AFL when starting up
        '''

        self.binary_path = binary_path
        self.work_dir    = work_dir
        self.afl_count   = afl_count
        self.seeds       = ["fuzz"] if seeds is None else seeds

        # binary id
        self.binary_id = os.path.basename(binary_path)

        self.job_dir  = os.path.join(self.work_dir, self.binary_id)
        self.in_dir   = os.path.join(self.job_dir, "input")
        self.out_dir  = os.path.join(self.job_dir, "sync")

        # sanity check extra opts
        self.extra_opts = extra_opts
        if self.extra_opts is not None:
            if not isinstance(self.extra_opts, list):
                raise ValueError("extra_opts must be a list of command line arguments")

        # base of the fuzzer package
        self.base = os.path.join(os.path.dirname(__file__), "..")

        self.start_time       = int(time.time())
        # the path to AFL capable of calling driller
        self.afl_path         = os.path.join(self.base, "bin", "afl", "afl-fuzz")
        # create_dict script
        self.create_dict_path = os.path.join(self.base, "bin", "create_dict.py")
        # afl dictionary
        self.dictionary       = None
        # processes spun up
        self.procs            = [ ]
        # start the fuzzer ids at 0
        self.fuzz_id          = 0
        # test if we're resuming an old run
        self.resuming         = bool(os.listdir(self.in_dir)) if os.path.isdir(self.in_dir) else False
        # has the fuzzer been turned on?
        self._on = False

        # the AFL build path for afl-qemu-trace-*
        qemu_dir = angr.Project(binary_path).arch.qemu_name
        self.afl_path_var     = os.path.join(self.base, "bin", "afl", "tracers", qemu_dir)
        self.qemu_dir         = self.afl_path_var

        l.debug("self.start_time: %r", self.start_time)
        l.debug("self.afl_path: %s", self.afl_path)
        l.debug("self.afl_path_var: %s", self.afl_path_var)
        l.debug("self.qemu_dir: %s", self.qemu_dir)
        l.debug("self.binary_id: %s", self.binary_id)
        l.debug("self.work_dir: %s", self.work_dir)
        l.debug("self.resuming: %s", self.resuming)

        # if we're resuming an old run set the input_directory to a '-'
        if self.resuming:
            l.info("[%s] resuming old fuzzing run", self.binary_id)
            self.in_dir = "-"

        else:
            # create the work directory and input directory
            try:
                os.makedirs(self.in_dir)
            except OSError:
                l.warning("unable to create in_dir \"%s\"", self.in_dir)

            # populate the input directory
            self._initialize_seeds()

        # look for a dictionary
        dictionary_file = os.path.join(self.job_dir, "%s.dict" % self.binary_id)
        if os.path.isfile(dictionary_file):
            self.dictionary = dictionary_file

        # if a dictionary doesn't exist and we aren't resuming a run, create a dict
        elif not self.resuming:
            # call out to another process to create the dictionary so we can
            # limit it's memory
            if create_dictionary:
                if self._create_dict(dictionary_file):
                    self.dictionary = dictionary_file
                else:
                    # no luck creating a dictionary
                    l.warning("[%s] unable to create dictionary", self.binary_id)

        # set environment variable for the AFL_PATH
        os.environ['AFL_PATH'] = self.afl_path_var


    ### EXPOSED
    def start(self):
        '''
        start fuzzing
        '''

        # test to see if the binary is so bad it crashes on our test case
        if self._crash_test():
            raise EarlyCrash

        # spin up the AFL workers
        self._start_afl()

        self._on = True

    @property
    def alive(self):
        if not self._on:
            return False

        alive_cnt = 0
        if self._on:
            stats = self.stats()
            for fuzzer in stats:
                try:
                    os.kill(int(stats[fuzzer]['fuzzer_pid']), 0)
                    alive_cnt += 1
                except OSError:
                    pass

        return bool(alive_cnt)

    def kill(self):
        for p in self.procs:
            p.terminate()
            p.wait()

        self._on = False

    def stats(self):

        # collect stats into dictionary
        stats = {}
        if os.path.isdir(self.out_dir):
            for fuzzer_dir in os.listdir(self.out_dir):
                stat_path = os.path.join(self.out_dir, fuzzer_dir, "fuzzer_stats")
                if os.path.isfile(stat_path):
                    stats[fuzzer_dir] = {}

                    with open(stat_path, "rb") as f:
                        stat_blob = f.read()
                        stat_lines = stat_blob.split("\n")[:-1]
                        for stat in stat_lines:
                            key, val = stat.split(":")
                            stats[fuzzer_dir][key.strip()] = val.strip()

        return stats

    def found_crash(self):

        stats = self.stats()

        for job in stats:
            try:
                if int(stats[job]['unique_crashes']) > 0:
                    return True
            except KeyError:
                pass

        return False

    def add_fuzzer(self):

        self.procs.append(self._start_afl_instance())

    def add_fuzzers(self, n):
        for _ in range(n):
            self.add_fuzzer()

    def crashes(self):
        '''
        retrieve the crashes discovered by AFL
        :return: a list of strings which are crashing inputs
        '''

        if not self.found_crash():
            return [ ]

        crashes = set()
        for fuzzer in os.listdir(self.out_dir):
            crashes_dir = os.path.join(self.out_dir, fuzzer, "crashes")

            if not os.path.isdir(crashes_dir):
                # if this entry doesn't have a crashes directory, just skip it
                continue

            for crash in os.listdir(crashes_dir):
                if crash == "README.txt":
                    # skip the readme entry
                    continue

                crash_path = os.path.join(crashes_dir, crash)
                with open(crash_path, 'rb') as f:
                    crashes.add(f.read())

        return list(crashes)

    ### FUZZ PREP

    def _initialize_seeds(self):
        '''
        populate the input directory with the seeds specified
        '''

        assert len(self.seeds) > 0, "Must specify at least one seed to start fuzzing with"

        l.debug("initializing seeds %r", self.seeds)

        template = os.path.join(self.in_dir, "seed-%d")
        for i, seed in enumerate(self.seeds):
            with open(template % i, "wb") as f:
                f.write(seed)

    ### DICTIONARY CREATION

    def _create_dict(self, dict_file):

        l.debug("creating a dictionary of string references within binary \"%s\"",
                self.binary_id)

        args = [self.create_dict_path, self.binary_path, dict_file]

        p = subprocess.Popen(args)
        retcode = p.wait()

        return True if retcode == 0 else False

    ### BEHAVIOR TESTING

    def _crash_test(self):

        args = [os.path.join(self.qemu_dir, "afl-qemu-trace"), self.binary_path]

        fd, jfile = tempfile.mkstemp()
        os.close(fd)

        with open(jfile, 'w') as f:
            f.write("fuzz")

        with open(jfile, 'r') as i:
            with open('/dev/null', 'w') as o:
                p = subprocess.Popen(args, stdin=i, stdout=o)
                p.wait()

                if p.poll() < 0:
                    ret = True
                else:
                    ret = False

        os.remove(jfile)
        return ret

    ### AFL SPAWNERS

    def _start_afl_instance(self, memory="8G"):

        args = [self.afl_path]

        args += ["-i", self.in_dir]
        args += ["-o", self.out_dir]
        args += ["-m", memory]
        args += ["-Q"]
        if self.fuzz_id == 0:
            args += ["-M", "fuzzer-master"]
            outfile = "fuzzer-master.log"
        else:
            args += ["-S", "fuzzer-%d" % self.fuzz_id]
            outfile = "fuzzer-%d.log" % self.fuzz_id

        if self.dictionary is not None:
            args += ["-x", self.dictionary]

        if self.extra_opts is not None:
            args += self.extra_opts

        args += ["--", self.binary_path]

        l.debug("execing: %s > %s", ' '.join(args), outfile)

        outfile = os.path.join(self.job_dir, outfile)
        fp = open(outfile, "w")

        # increment the fuzzer ID
        self.fuzz_id += 1

        return subprocess.Popen(args, stdout=fp)

    def _start_afl(self):
        '''
        start up a number of AFL instances to begin fuzzing
        '''

        # spin up the master AFL instance
        master = self._start_afl_instance() # the master fuzzer
        self.procs.append(master)

        if self.afl_count > 1:
            driller = self._start_afl_instance()
            self.procs.append(driller)

        # only spins up an AFL instances if afl_count > 1
        for _ in range(2, self.afl_count):
            slave = self._start_afl_instance()
            self.procs.append(slave)