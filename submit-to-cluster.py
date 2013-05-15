#!./python_env/bin/python

import sys
sys.path.append('./include')
import abstract_step
import fscache
import pipeline
import datetime
import copy
import os
import re
import subprocess
import yaml

'''
By default, this script submits all tasks to a Sun GridEngine cluster via
qsub. The list of tasks can be narrowed down by specifying a step name
(in which case all runs of this steps will be considered) or individual
tasks (step_name/run_id).

At this point, we have a task wish list.

This task wish list is now processed one by one (in topological order):

- determine status of current task
- if it's finished or running or queued, skip it
- if it's ready, submit it
- if it's waiting, there is at least one parent task which is not yet finished
- determine all parent tasks which are not finished yet, for each parent:
  - if it's running or queued, determine its job_id from the queued-ping file
  - if it's ready or waiting, we must skip this task because it should have been 
    enqueued a couple of iterations ago (this is because a selection has been
    made and there are unfinished, non-running, unqueued dependencies)
  - now add all these collected job_ids to the submission via -hold_jid
'''

def main():
    original_argv = copy.copy(sys.argv)

    p = pipeline.Pipeline()
    
    use_highmem = False
    if '--highmem' in sys.argv:
        use_highmem = True
        sys.argv.remove('--highmem')

    task_wish_list = None
    if len(sys.argv) > 1:
        task_wish_list = list()
        for _ in sys.argv[1:]:
            if '/' in _:
                task_wish_list.append(_)
            else:
                for task in p.all_tasks_topologically_sorted:
                    if str(task)[0:len(_)] == _:
                        task_wish_list.append(str(task))

    tasks_left = []

    template = open('qsub-template.sh', 'r').read()

    for task in p.all_tasks_topologically_sorted:
        if task_wish_list is not None:
            if not str(task) in task_wish_list:
                continue
        tasks_left.append(task)
                
    print("Now attempting to submit %d jobs..." % len(tasks_left))

    quotas = dict()
    quotas['default'] = 5

    # read quotas
    # -> for every step, a quota can be defined (with a default quota in place for steps
    #    which have no defined quota)
    # -> during submitting, there is a list of N previous job ids in which every item
    #    holds one of the previously submitted tasks
    if os.path.exists("quotas.yaml"):
        all_quotas = yaml.load(open("quotas.yaml", 'r'))
        hostname = subprocess.check_output(['hostname']).strip()
        for key in all_quotas.keys():
            if re.match(key, hostname):
                print("Applying quotas for " + hostname + ".")
                quotas = all_quotas[key]

    if not 'default' in quotas:
        raise StandardError("No default quota defined for this host.")

    quota_jids = {}
    quota_offset = {}

    def submit_task(task, dependent_tasks_in = []):
        dependent_tasks = copy.copy(dependent_tasks_in)

        step_name = task.step.get_step_name()
        if not step_name in quota_jids:
            size = quotas[step_name] if step_name in quotas else quotas['default']
            quota_jids[step_name] = [None for _ in range(size)]
            quota_offset[step_name] = 0

        quota_predecessor = quota_jids[step_name][quota_offset[step_name]]
        if quota_predecessor:
            dependent_tasks.append(quota_predecessor)

        submit_script = copy.copy(template)
        submit_script = submit_script.replace("#{CORES}", str(task.step._cores))
        email = 'nobody@example.com'
        if 'email' in p.config:
            email = p.config['email']
        submit_script = submit_script.replace("#{EMAIL}", email)
        args = ['./run-locally.py']
        if '--even-if-dirty' in original_argv:
            args.append('--even-if-dirty')
        args.append('"' + str(task) + '"')
        submit_script = submit_script.replace("#{COMMAND}", ' '.join(args))

        long_task_id_with_run_id = '%s_%s' % (str(task.step), task.run_id)
        long_task_id = str(task.step)
        short_task_id = long_task_id[0:15]

        qsub_args = ['qsub', '-N', short_task_id]
        if use_highmem:
            qsub_args.append('-q')
            qsub_args.append('highmem')
            
        qsub_args.append('-e')
        qsub_args.append(os.path.join(task.step.get_output_directory(), '.' + long_task_id_with_run_id + '.stderr'))
        qsub_args.append('-o')
        qsub_args.append(os.path.join(task.step.get_output_directory(), '.' + long_task_id_with_run_id + '.stdout'))

        # create the output directory if it doesn't exist yet
        # this is necessary here because otherwise, qsub will complain
        if not os.path.isdir(task.step.get_output_directory()):
            os.makedirs(task.step.get_output_directory())

        if len(dependent_tasks) > 0:
            qsub_args.append("-hold_jid")
            qsub_args.append(','.join(dependent_tasks))

        really_submit_this = True
        if task_wish_list:
            if not str(task) in task_wish_list:
                really_submit_this = False
        if task.get_task_state() == p.states.EXECUTING:
            print("Skipping %s because it is already executing." % str(task))
            really_submit_this = False
        if really_submit_this:
            sys.stdout.write("Submitting task " + str(task) + " with " + str(task.step._cores) + " cores => ")
            process = subprocess.Popen(qsub_args, bufsize = -1, stdin = subprocess.PIPE, stdout = subprocess.PIPE)
            process.stdin.write(submit_script)
            process.stdin.close()
            process.wait()
            response = process.stdout.read()
            job_id = re.search('Your job (\d+)', response).group(1)
            if job_id == None or len(job_id) == 0:
                raise StandardError("Error: We couldn't parse a job_id from this:\n" + response)
            
            queued_ping_info = dict()
            queued_ping_info['step'] = str(task.step)
            queued_ping_info['run_id'] = task.run_id
            queued_ping_info['job_id'] = job_id
            queued_ping_info['submit_time'] = datetime.datetime.now()
            with open(task.step.get_queued_ping_path_for_run_id(task.run_id), 'w') as f:
                f.write(yaml.dump(queued_ping_info, default_flow_style = False))

            quota_jids[step_name][quota_offset[step_name]] = job_id
            quota_offset[step_name] = (quota_offset[step_name] + 1) % len(quota_jids[step_name])

            print("%s (%s)" % (job_id, short_task_id))
            if len(dependent_tasks) > 0:
                print(" - with dependent tasks: " + ', '.join(dependent_tasks))
                
            abstract_step.AbstractStep.fsc = fscache.FSCache()

    for task in tasks_left:
        state = task.get_task_state()
        if state in [p.states.QUEUED, p.states.EXECUTING, p.states.FINISHED]:
            print("Skipping %s because it is already %s..." % (str(task), state.lower()))
            continue
        if state == p.states.READY:
            submit_task(task)
        if state == p.states.WAITING:
            parent_job_ids = list()
            for parent_task in task.get_parent_tasks():
                parent_state = parent_task.get_task_state()
                if parent_state in [p.states.EXECUTING, p.states.QUEUED]:
                    # determine job_id from YAML queued ping file
                    parent_job_id = None
                    parent_queued_ping_path = parent_task.step.get_queued_ping_path_for_run_id(parent_task.run_id)
                    try:
                        parent_info = yaml.load(open(parent_queued_ping_path))
                        parent_job_ids.append(parent_info['job_id'])
                    except:
                        print("Couldn't determine job_id of %s while trying to load %s." % 
                            (parent_task, parent_queued_ping_path))
                        raise
                elif parent_state in [p.states.READY, p.states.WAITING]:
                    raise StandardError("Cannot submit %s because its "
                        "parent %s is %s when it should be queued, running, "
                        "or finished and the task selection as defined by "
                        "your command-line arguments do not request it to "
                        "submitted." % (task, parent_task, parent_state.lower()))
            submit_task(task, parent_job_ids)

if __name__ == '__main__':
    main()
