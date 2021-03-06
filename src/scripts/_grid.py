import sys
sys.path.append('../..')
def generate_all_commands(command):

    # split into list of args
    arg_strs = command.split(' -')
    for i, s in enumerate(arg_strs):
        new_s = '-'+s
        arg_strs[i] = new_s

    # the number of jobs. remove the dash
    arg_strs[0] = arg_strs[0][1:]

    # split into name/value pairs
    args_dict = {}
    for i, s in enumerate(arg_strs):
        try:
            # handle possible whitespace between comma-separated params
            s_split = s.split(' ')
            name, value = s_split[0], ' '.join(s_split[1:])
        except ValueError:
            name, value = s, ''
        args_dict[name] = value

    # split into single and multi args
    single_args = []
    multi_args_dict = {}
    for name, value in args_dict.items():
        if ',' in value:
            multi_args_dict[name] = value.split(',')
            #print(value)
        else:
            single_args.append(name + ' ' + value)

    # generate all commands
    start_command = " ".join(single_args)
    commands = [start_command]

    for name, values in multi_args_dict.items():
        new_cmds = []
        for cmd in commands:
            for v in values:
                new_cmd = cmd + ' ' + name + ' ' + v
                new_cmds.append(new_cmd)
        commands = new_cmds

    return '\n'.join(commands)



if __name__ == '__main__':
    cmd = ' '.join(sys.argv[1:])
    commands = generate_all_commands(cmd)
    print(commands)
