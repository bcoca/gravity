#!/usr/bin/env python

# also dynamically imports ansible in code

import argparse
import configparser
import glob
import os
import re
import shutil
import subprocess
import sys
import yaml

from collections import defaultdict
from collections.abc import Mapping, Sequence
from importlib import import_module
from string import Template

from ansible.vars.reserved import is_reserved_name
from logzero import logger

from baron.parser import ParsingError
import redbaron


# https://github.com/ansible/ansible/blob/100fe52860f45238ee8ca9e3019d1129ad043c68/hacking/fix_test_syntax.py#L62
FILTER_RE = re.compile(r'((.+?)\s*([\w \.\'"]+)(\s*)\|(\s*)(\w+))')
TEST_RE = re.compile(r'((.+?)\s*([\w \.\'"]+)(\s*)is(\s*)(\w+))')

DEVEL_URL = 'https://github.com/ansible/ansible.git'
DEVEL_BRANCH = 'devel'

VARDIR = os.environ.get('GRAVITY_VAR_DIR', '.cache')
COLLECTION_NAMESPACE = 'test_migrate_ns'
PLUGIN_EXCEPTION_PATHS = {'modules': 'lib/ansible/modules', 'module_utils': 'lib/ansible/module_utils', 'lookups': 'lib/ansible/plugins/lookup'}


RAW_STR_TMPL = "r'''{str_val}'''"
STR_TMPL = "'''{str_val}'''"


core = {}

def add_core(ptype, name):

    global core
    if ptype not in core:
        core[ptype] = set()

    core[ptype].add(name)


def _run_command(cmd=None, check_rc=True):
    logger.debug(cmd)
    if not isinstance(cmd, bytes):
        cmd = cmd.encode('utf-8')
    p = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    (so, se) = p.communicate()

    if check_rc and p.returncode != 0:
        raise RuntimeError(se)

    so = so.decode('utf-8')
    se = se.decode('utf-8')

    return (p.returncode, so, se)


def run_command(cmd=None, check_rc=True):
    (rc, so, se) = _run_command(cmd, check_rc)
    return {
        'rc': rc,
        'so': so,
        'se': se
    }


def checkout_repo(vardir=VARDIR, refresh=False):
    releases_dir = os.path.join(vardir, 'releases')
    devel_path = os.path.join(releases_dir, f'{DEVEL_BRANCH}.git')

    if refresh and os.path.exists(devel_path):
        # TODO do we want/is it worth to use a git library instead?
        cmd = 'cd %s; git checkout %s; git pull' % (devel_path, DEVEL_BRANCH)
        rc, stdout, stderr = _run_command(cmd)

    if not os.path.exists(releases_dir):
        os.makedirs(releases_dir)

    if not os.path.exists(devel_path):
        cmd = 'git clone %s %s; cd %s; git checkout %s' % (DEVEL_URL, devel_path, devel_path, DEVEL_BRANCH)
        rc, stdout, stderr = _run_command(cmd)


def read_yaml_file(path):
    with open(path, 'rb') as yaml_file:
        return yaml.safe_load(yaml_file)


def write_yaml_into_file_as_is(path, data):
    yaml_text = yaml.dump(data, default_flow_style=False, sort_keys=False)
    write_text_into_file(path, yaml_text)


def load_spec_file(spec_file):

    spec = read_yaml_file(spec_file)  # TODO: capture yamlerror?

    if not isinstance(spec, Mapping):
        sys.exit("Invalid format for spec file, expected a dictionary and got %s" % type(spec))
    elif not spec:
        sys.exit("Cannot use spec file, ended up with empty spec")

    return spec


def clean_extra_lines(rawtext):
    lines = rawtext.split('\n')

    imports_start = None
    imports_stop = None
    for idx, x in enumerate(lines):
        if imports_start is None:
            if x.startswith('from ') and not 'absolute_import' in x:
                imports_start = idx
                continue

        if not x:
            continue

        if x.startswith('from '):
            continue

        if imports_start and imports_stop is None:
            if x[0].isalnum():
                imports_stop = idx
                break

    empty_lines = [x for x in range(imports_start, imports_stop)]
    empty_lines = [x for x in empty_lines if not lines[x].strip()]

    if not empty_lines:
        return rawtext

    if len(empty_lines) == 1:
        return rawtext

    # keep 2 empty lines between imports and definitions
    if len(empty_lines) == 2 and (empty_lines[-1] - empty_lines[-2] == 1):
        return rawtext

    print(lines[imports_start:imports_stop])

    while empty_lines:
        try:
            print('DELETING: %s' % lines[empty_lines[0]])
        except IndexError as e:
            print(e)
            import epdb; epdb.st()
        del lines[empty_lines[0]]
        del empty_lines[0]
        empty_lines = [x-1 for x in empty_lines]
        if [x for x in empty_lines if x <= 0]:
            break

        if len(empty_lines) <= 2:
            break

        #import epdb; epdb.st()

    rawtext = '\n'.join(lines)
    return rawtext


def get_plugin_collection(plugin_name, plugin_type, spec):
    for collection in spec.keys():
        if spec[collection]: # avoid empty collections
            plugins = spec[collection].get(plugin_type, [])
            if plugin_name + '.py' in plugins:
                return collection

    # keep info
    plugin_name = plugin_name.replace('/', '.')
    logger.debug('Assuming "%s.%s " stays in core' % (plugin_type, plugin_name))
    add_core(plugin_type, plugin_name.replace('/', '.'))

    raise LookupError('Could not find "%s" named "%s" in any collection in the spec' % (plugin_type, plugin_name))


def get_plugins_from_collection(collection, plugin_type, spec):
    assert collection in spec
    return [plugin.rsplit('/')[-1][:-3] for plugin in spec[collection].get(plugin_type, [])]


def get_plugin_fqcn(namespace, collection, plugin_name):
    return '%s.%s.%s' % (namespace, collection, plugin_name)


def rewrite_doc_fragments(mod_fst, collection, spec, namespace):
    try:
        doc_val = (
            mod_fst.
            find_all('assignment').
            find('name', value='DOCUMENTATION').
            parent.
            value
        )
    except AttributeError:
        raise LookupError('No DOCUMENTATION found')

    doc_str_tmpl = RAW_STR_TMPL if doc_val.type == 'raw_string' else STR_TMPL
    # Turn `'strng'` into `strng` and  `r'strng'` into `strng`
    # so that we don't feed a quoted string into the YAML parser:
    doc_txt = doc_val.to_python()

    docs_parsed = yaml.safe_load(doc_txt.strip('\n'))

    fragments = docs_parsed.get('extends_documentation_fragment', [])
    if not isinstance(fragments, list):
        fragments = [fragments]

    deps = []
    for fragment in fragments:
        # some doc_fragments use subsections (e.g. vmware.vcenter_documentation)
        fragment_name, _dot, _rest = fragment.partition('.')
        try:
            fragment_collection = get_plugin_collection(fragment_name, 'doc_fragments', spec)
        except LookupError:
            # plugin not in spec, assuming it stays in core and leaving as is
            continue

        if fragment_collection.startswith('_'):
            # skip rewrite
            continue

        # TODO what if it's in a different namespace (different spec)? do we care?
        new_fragment = f'{namespace}.{fragment_collection}.{fragment}'

        # `doc_val` holds a baron representation of the string node
        # of type 'string' or 'raw_string'. Updating its `.value`
        # via assigning the new one replaces the node in FST.
        # Also, in order to generate a string or raw-string literal,
        # we need to wrap it with a corresponding pair of quotes.
        # If we don't do this, we'd generate the following Python code
        # ```
        # DOCUMENTATION = some string value
        # ```
        # instead of the correct
        # ```
        # DOCUMENTATION = r'''some string value'''
        # ```
        # or
        # ```
        # DOCUMENTATION = '''some string value'''
        # ```
        # TODO figure out whether this can be improved
        doc_val.value = doc_str_tmpl.format(
            str_val=doc_txt.replace(fragment, new_fragment),
        )

        if collection != fragment_collection:
            deps.append(fragment_collection)

    return deps


def rewrite_imports(mod_fst, collection, spec, namespace):
    """Rewrite imports map."""
    plugins_path = ('ansible_collections', namespace, collection, 'plugins')
    import_map = {
        ('ansible', 'module_utils'): plugins_path + ('module_utils', ),
        ('ansible', 'plugins'): plugins_path,
    }

    return rewrite_imports_in_fst(mod_fst, import_map, collection, spec)


def match_import_src(imp_src, import_map):
    """Find a replacement map entry matching the current import."""
    imp_src_tuple = tuple(t.value for t in imp_src)
    for old_imp, new_imp in import_map.items():
        token_length = len(old_imp)
        if imp_src_tuple[:token_length] != old_imp:
            continue
        return token_length, new_imp

    raise LookupError(f"Couldn't find a replacement for {imp_src!s}")


def rewrite_imports_in_fst(mod_fst, import_map, collection, spec):
    """Replace imports in the python module FST."""
    deps = []
    for imp in mod_fst.find_all(('import', 'from_import')):
        imp_src = imp.value
        if imp.type == 'import':
            imp_src = imp_src[0].value

        try:
            token_length, exchange = match_import_src(imp_src, import_map)
        except LookupError:
            continue  # no matching imports

        if len(imp.find_all('name_as_name', value='g:*Base*')) > 0:
            continue  # Skip imports of Base classes
        if len(imp.find_all('name_as_name', value='g:*loader*')) > 0:
            continue  # Skip imports of ansible.plugin.loader.py

        if imp_src[1].value == 'module_utils':
            plugin_type = 'module_utils'
            plugin_name = '/'.join(t.value for t in imp_src[token_length:])
        elif imp_src[1].value == 'plugins':
            try:
                plugin_type = imp_src[2].value
                plugin_name = imp_src[3].value
            except IndexError:
                # FIXME logging an error to investigate for now
                # one example I found is: from ansible.plugins.cache import CachePluginAdjudicator as CacheObject
                logger.error('Could not get plugin type or name from ' + str(imp) + '. Is this expected?')
                continue
        else:
            raise Exception('BUG: Could not process import: ' + str(imp))

        try:
            plugin_collection = get_plugin_collection(plugin_name, plugin_type, spec)
        except LookupError as e:
            # plugin not in spec, assuming it stays in core and skipping
            continue

        if plugin_collection.startswith('_'):
            # skip rewrite
            continue

        imp_src[:token_length] = exchange  # replace the import
        if plugin_collection != collection:
            imp_src[2] = plugin_collection
            deps.append(plugin_collection)

    return deps


def read_text_from_file(path):
    with open(path, 'r') as f:
        return f.read()


def write_text_into_file(path, text):
    with open(path, 'w') as f:
        return f.write(text)


def read_module_txt_n_fst(path):
    """Parse module source code in form of Full Syntax Tree."""
    mod_src_text = read_text_from_file(path)
    try:
        return mod_src_text, redbaron.RedBaron(mod_src_text)
    except ParsingError:
        logger.exception('failed parsing on %s', mod_src_text)
        raise


def resolve_spec(spec, checkoutdir):

    # TODO: add negation? entry: x/* \n entry: !x/base.py
    for coll in spec.keys():
        for ptype in spec[coll].keys():
            plugin_base = os.path.join(checkoutdir, PLUGIN_EXCEPTION_PATHS.get(ptype, os.path.join('lib', 'ansible', 'plugins', ptype)))
            replace_base = '%s/' % plugin_base
            for entry in spec[coll][ptype]:
                if r'*' in entry or r'?' in entry:
                    files = glob.glob(os.path.join(plugin_base, entry))
                    for fname in files:
                        if ptype != 'module_utils' and fname.endswith('__init__.py') or not os.path.isfile(fname):
                            continue
                        fname = fname.replace(replace_base, '')
                        spec[coll][ptype].append(fname)

                    # clean out glob entry
                    spec[coll][ptype].remove(entry)


def assemble_collections(spec, args):
    # NOTE releases_dir is already created by checkout_repo(), might want to move all that to something like ensure_dirs() ...
    releases_dir = os.path.join(args.vardir, 'releases')
    checkout_path = os.path.join(releases_dir, f'{DEVEL_BRANCH}.git')
    collections_base_dir = os.path.join(args.vardir, 'collections')
    meta_dir = os.path.join(args.vardir, 'meta')
    integration_test_dirs = []

    resolve_spec(spec, checkout_path)

    if args.refresh and os.path.exists(collections_base_dir):
        shutil.rmtree(collections_base_dir)

    # make initial YAML transformation to minimize the diff
    mark_moved_resources(checkout_path, 'init', set())

    seen = {}
    migrated_to_collection = defaultdict(set)
    for collection in spec.keys():

        if collection.startswith('_'):
            # these are placeholder collections
            continue

        collection_dir = os.path.join(collections_base_dir, 'ansible_collections', args.namespace, collection)

        if args.refresh and os.path.exists(collection_dir):
            shutil.rmtree(collection_dir)

        if not os.path.exists(collection_dir):
            os.makedirs(collection_dir)

        # create the data for galaxy.yml
        galaxy_metadata = {
            'namespace': args.namespace,
            'name': collection,
            'version': '1.0.0',  # TODO: add to spec, args?
            'readme': None,
            'authors': None,
            'description': None,
            'license': None,
            'license_file': None,
            'tags': None,
            'dependencies': {},
            'repository': None,
            'documentation': None,
            'homepage': None,
            'issues': None
        }

        for plugin_type in spec[collection].keys():

            # get right plugin path
            if plugin_type not in PLUGIN_EXCEPTION_PATHS:
                src_plugin_base = os.path.join('lib', 'ansible', 'plugins', plugin_type)
            else:
                src_plugin_base = PLUGIN_EXCEPTION_PATHS[plugin_type]

            # ensure destinations exist
            dest_plugin_base = os.path.join(collection_dir, 'plugins', plugin_type)
            if not os.path.exists(dest_plugin_base):
                os.makedirs(dest_plugin_base)
                with open(os.path.join(dest_plugin_base, '__init__.py'), 'w') as f:
                    f.write('')

            # process each plugin
            for plugin in spec[collection][plugin_type]:
                plugin_sig = '%s/%s' % (plugin_type, plugin)
                if plugin_sig in seen:
                    raise ValueError(
                        'Each plugin needs to be assigned to one collection '
                        f'only. {plugin_sig} has already been processed as a '
                        f'part of `{seen[plugin_sig]}` collection.'
                    )
                seen[plugin_sig] = collection

                # TODO: currently requires 'full name of file', but should work w/o extension?
                src = os.path.join(checkout_path, src_plugin_base, plugin)
                migrated_to_collection[collection].add(os.path.join(src_plugin_base, plugin))
                if (args.preserve_module_subdirs and plugin_type == 'modules') or plugin_type == 'module_utils':
                    dest = os.path.join(dest_plugin_base, plugin)
                    dest_dir = os.path.dirname(dest)
                    if not os.path.exists(dest_dir):
                        os.makedirs(dest_dir)
                else:
                    dest = os.path.join(dest_plugin_base, os.path.basename(plugin))

                if not os.path.exists(src):
                    raise Exception('Spec specifies "%s" but file "%s" is not found in checkout' % (plugin, src))

                if os.path.islink(src):
                    shutil.copyfile(src, dest, follow_symlinks=False)
                    continue
                elif not src.endswith('.py'):
                    # its not all python files, copy and go to next
                    # TODO: handle powershell import rewrites
                    shutil.copyfile(src, dest)
                    continue

                mod_src_text, mod_fst = read_module_txt_n_fst(src)

                # were any lines nullified?
                #extralines = False

                import_dependencies = rewrite_imports(mod_fst, collection, spec, args.namespace)
                try:
                    docs_dependencies = rewrite_doc_fragments(mod_fst, collection, spec, args.namespace)
                except LookupError as err:
                    logger.info('%s in %s', err, src)
                plugin_data_new = mod_fst.dumps()

                # clean too many empty lines
                #if extralines:
                #    data = clean_extra_lines(data)

                if mod_src_text != plugin_data_new:
                    for dep in docs_dependencies + import_dependencies:
                        dep_collection = '%s.%s' % (args.namespace, dep)
                        # FIXME hardcoded version
                        galaxy_metadata['dependencies'][dep_collection] = '>=1.0'
                    logger.info('rewriting plugin references in %s' % dest)

                write_text_into_file(dest, plugin_data_new)

                integration_test_dirs.extend(poor_mans_integration_tests_discovery(checkout_path, plugin_type, plugin))
                # process unit tests TODO: sanity? , integration?
                #copy_unit_tests(plugin, collection, spec, args)

        # FIXME need to hack PyYAML to preserve formatting (not how much it's possible or how much it is work) or use e.g. ruamel.yaml
        rewrite_integration_tests(integration_test_dirs, checkout_path, collection_dir, args.namespace, collection, spec)

        global integration_tests_deps
        for dep in integration_tests_deps:
            dep_collection = '%s.%s' % (args.namespace, dep)
            # FIXME hardcoded version
            galaxy_metadata['dependencies'][dep_collection] = '>=1.0'

        integration_test_dirs = []
        integration_tests_deps = set()

        # write collection metadata
        write_yaml_into_file_as_is(
            os.path.join(collection_dir, 'galaxy.yml'),
            galaxy_metadata,
        )

        # init git repo
        subprocess.check_call(('git', 'init'), cwd=collection_dir)
        subprocess.check_call(('git', 'add', '.'), cwd=collection_dir)
        subprocess.check_call(
            ('git', 'commit', '-m', 'Initial commit', '--allow-empty'),
            cwd=collection_dir,
        )

        mark_moved_resources(
            checkout_path, collection, migrated_to_collection[collection],
        )


def mark_moved_resources(checkout_dir, collection, migrated_to_collection):
    """Mark migrated paths in botmeta."""
    moved_collection_url = (
        f'https://github.com/ansible-collections/{collection}'
    )
    botmeta_rel_path = '.github/BOTMETA.yml'
    botmeta_checkout_path = os.path.join(checkout_dir, botmeta_rel_path)
    close_related_issues = False

    botmeta = read_yaml_file(botmeta_checkout_path)

    botmeta_files = botmeta['files']
    botmeta_file_paths = botmeta_files.keys()
    botmeta_macros = botmeta['macros']

    transformed_path_key_map = {}
    for k in botmeta_file_paths:
        transformed_key = Template(k).substitute(**botmeta_macros)
        if transformed_key == k:
            continue
        transformed_path_key_map[transformed_key] = k

    for migrated_resource in migrated_to_collection:
        macro_path = transformed_path_key_map.get(
            migrated_resource, migrated_resource,
        )

        migrated_secion = botmeta_files.get(macro_path)
        if not migrated_secion:
            migrated_secion = botmeta_files[macro_path] = {}
        elif isinstance(migrated_secion, str):
            migrated_secion = botmeta_files[macro_path] = {
                'maintainers': migrated_secion,
            }

        migrated_secion['close'] = close_related_issues
        migrated_secion['moved'] = moved_collection_url

    write_yaml_into_file_as_is(botmeta_checkout_path, botmeta)

    # Commit changes to the migrated Git repo
    subprocess.check_call(
        ('git', 'add', f'{botmeta_rel_path!s}'),
        cwd=checkout_dir,
    )
    subprocess.check_call(
        (
            'git', 'commit',
            '-m', f'Mark migrated {collection}',
            '--allow-empty',
        ),
        cwd=checkout_dir,
    )


def copy_tests(plugin, coll, spec, args):

    # TODO: tests might also require rewriting imports, docfragments and even play/tasks,
    #  why i made functions above from preexisting code
    return

    # UNIT TESTS
    # need to fix these imports in the unit tests

    dst = os.path.join(plugin, 'test', 'unit')
    if not os.path.exists(dst):
        os.makedirs(dst)
    for uf in spec['units']:  # TODO: should we rely on spec or 'autofind' matching units of same name/type?
        fuf = os.path.join(args.vardir, 'test', 'units', uf)
        if os.path.isdir(fuf):
            #import epdb; epdb.st()

            fns = glob.glob('%s/*' % fuf)
            for fn in fns:
                if os.path.isdir(fn):
                    try:
                        shutil.copytree(fn, os.path.join(dst, os.path.basename(fn)))
                    except Exception as e:
                        pass
                else:
                    shutil.copy(fn, os.path.join(dst, os.path.basename(fn)))


        elif os.path.isfile(fuf):
            fuf_dst = os.path.join(dst, os.path.basename(fuf))
            shutil.copy(fuf, fuf_dst)

        cmd = 'find %s -type f -name "*.py"' % (dst)
        res = run_command(cmd)
        unit_files = sorted([x.strip() for x in res['so'].split('\n') if x.strip()])

        for unit_file in unit_files:
            # fix the module import paths to be relative
            #   from ansible.modules.cloud.vmware import vmware_guest
            #   from ...plugins.modules import vmware_guest

            depth = unit_file.replace(cdir, '')
            depth = depth.lstrip('/')
            depth = os.path.dirname(depth)
            depth = depth.split('/')
            rel_path = '.'.join(['' for x in range(-1, len(depth))])

            with open(unit_file, 'r') as f:
                unit_lines = f.readlines()
            unit_lines = [x.rstrip() for x in unit_lines]

            changed = False

            for module in module_names:
                for li,line in enumerate(unit_lines):
                    if line.startswith('from ') and line.endswith(module):
                        unit_lines[li] = 'from %s.plugins.modules import %s' % (rel_path, module)
                        changed = True

            if changed:
                with open(unit_file, 'w') as f:
                    f.write('\n'.join(unit_lines))
            #import epdb; epdb.st()


        list_of_targets = []  # TODO: same as above require from spec or find for ourselves?
        if list_of_targets:
            dst = os.path.join(cdir, 'test', 'integration', 'targets')
            if not os.path.exists(dst):
                os.makedirs(dst)
            for uf in v['targets']:
                fuf = os.path.join(args.vardir, 'test', 'integration', 'targets', uf)
                duf = os.path.join(dst, os.path.basename(fuf))
                if not os.path.exists(os.path.join(dst, os.path.basename(fuf))):
                    try:
                        shutil.copytree(fuf, duf)
                    except Exception as e:
                        import epdb; epdb.st()

                # set namespace for all module refs
                cmd = 'find %s -type f -name "*.yml"' % (duf)
                res = run_command(cmd)
                yfiles = res['so'].split('\n')
                yfiles = [x.strip() for x in yfiles if x.strip()]

                for yf in yfiles:
                    with open(yf, 'r') as f:
                        ydata = f.read()
                    _ydata = ydata[:]

                    for module in v['modules']:
                        msrc = os.path.basename(module)
                        msrc = msrc.replace('.py', '')
                        msrc = msrc.replace('.ps1', '')
                        msrc = msrc.replace('.ps2', '')

                        mdst = '%s.%s.%s' % (args.namespace, coll, msrc)

                        if msrc not in ydata or mdst in ydata:
                            continue

                        #import epdb; epdb.st()
                        ydata = ydata.replace(msrc, mdst)

                    # fix import_role calls?
                    #tasks = yaml.load(ydata)
                    #import epdb; epdb.st()

                    if ydata != _ydata:
                        logger.info('fixing module calls in %s' % yf)
                        with open(yf, 'w') as f:
                            f.write(ydata)

##############################################################################
# Rewrite integration tests
##############################################################################


def poor_mans_integration_tests_discovery(checkout_dir, plugin_type, plugin_name):
    # FIXME this might be actually enough for modules integration tests, at least for the most part
    if plugin_type != 'modules':
        return []

    files = glob.glob(os.path.join(checkout_dir, 'test/integration/targets', os.path.basename(os.path.splitext(plugin_name)[0])))
    for fname in files:
        logger.debug('Found integration tests for %s %s in %s' % (plugin_type, plugin_name, fname))

    return files

integration_tests_deps = set()

def rewrite_integration_tests(test_dirs, checkout_dir, collection_dir, namespace, collection, spec):
    # FIXME move to diff file
    # FIXME rewrite usage of modules in library/
    # FIXME module_defaults groups

    for test_dir in test_dirs:
        for dirpath, dirnames, filenames in os.walk(test_dir):
            for filename in filenames:
                full_path = os.path.join(dirpath, filename)
                logger.debug(full_path)

                dest_dir = os.path.join(collection_dir, os.path.relpath(dirpath, checkout_dir))
                if not os.path.exists(dest_dir):
                    os.makedirs(dest_dir)
                dest = os.path.join(dest_dir, filename)

                dummy, ext = os.path.splitext(filename)
                if ext in ('.yml', '.yaml'):
                    rewrite_yaml(full_path, dest, namespace, collection, spec)
                elif ext in ('.sh',):
                    rewrite_sh(full_path, dest, namespace, collection, spec)
                elif ext in ('.cfg',):
                    rewrite_ini(full_path, dest, namespace, collection, spec)
                else:
                    shutil.copy2(full_path, dest)


def rewrite_sh(full_path, dest, namespace, collection, spec):
    sh_key_map = {
        'ANSIBLE_CACHE_PLUGIN': 'cache',
        'ANSIBLE_CALLBACK_WHITELIST': 'callback',
        'ANSIBLE_INVENTORY_CACHE_PLUGIN': 'cache',
        'ANSIBLE_STDOUT_CALLBACK': 'callback',
        'ANSIBLE_STRATEGY': 'strategy',
        '--become-method': 'become',
        '-c': 'connection',
        '--connection': 'connection',
    }

    contents = read_text_from_file(full_path)
    for key, plugin_type in sh_key_map.items():
        if not contents.find(key):
            continue
        for coll in spec.keys():
            plugins = get_plugins_from_collection(coll, plugin_type, spec)
            for plugin_name in plugins:
                if not contents.find(plugin_name):
                    continue
                # FIXME list
                new_plugin_name = get_plugin_fqcn(namespace, coll, plugin_name)
                contents = contents.replace(key + '=' + plugin_name, key + '=' + new_plugin_name)
                contents = contents.replace(key + ' ' + plugin_name, key + ' ' + new_plugin_name)
                if collection != coll:
                    integration_tests_add_to_deps(collection, coll)

    write_text_into_file(dest, contents)
    shutil.copystat(full_path, dest)


def rewrite_ini(src, dest, namespace, collection, spec):
    ini_key_map = {
        'defaults': {
            'callback_whitelist': 'callback',
            'fact_caching': 'cache',
            'strategy': 'strategy',
            'stdout_callback': 'callback',
        },
        'inventory': {
            'cache_plugin': 'cache',
            'enable_plugins': 'inventory',
        }
    }

    config = configparser.ConfigParser()
    config.read(src)
    for section in config.sections():
        try:
            rewrite_ini_section(config, ini_key_map, section, namespace, collection, spec)
        except KeyError:
            continue

    with open(dest, 'w') as cf:
        config.write(cf)


def rewrite_ini_section(config, key_map, section, namespace, collection, spec):
    for keyword, plugin_type in key_map[section].items():
        try:
            # FIXME diff input format than csv?
            plugin_names = config.get(section, keyword).split(',')
        except configparser.NoOptionError:
            continue

        new_plugin_names = []
        for plugin_name in plugin_names:
            try:
                plugin_collection = get_plugin_collection(plugin_name, plugin_type, spec)
                new_plugin_names.append(get_plugin_fqcn(namespace, plugin_collection, plugin_name))
                if plugin_collection != collection:
                    integration_tests_add_to_deps(collection, plugin_collection)
            except LookupError:
                new_plugin_names.append(plugin_name)

        config.set(section, keyword, ','.join(new_plugin_names))


def rewrite_yaml(src, dest, namespace, collection, spec):
    contents = read_yaml_file(src)
    _rewrite_yaml(contents, namespace, collection, spec)
    write_yaml_into_file_as_is(dest, contents)


def _rewrite_yaml(contents, namespace, collection, spec):
    if isinstance(contents, list):
        for el in contents:
            _rewrite_yaml(el, namespace, collection, spec)
    elif isinstance(contents, Mapping):
        _rewrite_yaml_mapping(contents, namespace, collection, spec)


KEYWORD_TO_PLUGIN_MAP = {
    'ansible_become_method': 'become',
    'ansible_connection': 'connection',
    'ansible_shell_type': 'shell',
    'become_method': 'become',
    'cache_plugin': 'cache',
    'connection': 'connection',
    'plugin': 'inventory',
    'strategy': 'strategy',
}


def _rewrite_yaml_mapping(el, namespace, collection, spec):
    assert isinstance(el, Mapping)

    _rewrite_yaml_mapping_keys(el, namespace, collection, spec)
    _rewrite_yaml_mapping_values(el, namespace, collection, spec)


def _rewrite_yaml_mapping_keys(el, namespace, collection, spec):
    for key in el.keys():
        if is_reserved_name(key):
            logger.debug('skipping reserved name ' + key)
            continue

        plugin_type = KEYWORD_TO_PLUGIN_MAP.get(key)
        if plugin_type:
            try:
                plugin_collection = get_plugin_collection(el[key], plugin_type, spec)
                el[key] = get_plugin_fqcn(namespace, plugin_collection, el[key])
                if plugin_collection != collection:
                    integration_tests_add_to_deps(collection, plugin_collection)
            except LookupError:
                # TODO better var detection
                # TODO make a report of these at the end of the execution and/or into a file
                if '{{' in el[key]:
                    logger.error('could not rewrite "%s: %s"' % (key, el[key]))

        prefix = 'with_'
        if prefix in key:
            prefix_len = len(prefix)

            if not key.startswith(prefix):
                continue
            plugin_name = key[prefix_len:]
            try:
                plugin_collection = get_plugin_collection(plugin_name, 'lookup', spec)
                el[prefix + get_plugin_fqcn(namespace, plugin_collection, plugin_name)] = el[key]
                del el[key]
                if plugin_collection != collection:
                    integration_tests_add_to_deps(collection, plugin_collection)
            except LookupError:
                pass

        try:
            for coll in spec.keys():
                for module in get_plugins_from_collection(coll, 'modules', spec):
                    if key != module:
                        continue
                    new_module_name = get_plugin_fqcn(namespace, coll, key)
                    el[new_module_name] = el[key]
                    del el[key]
                    if coll != collection:
                        integration_tests_add_to_deps(collection, coll)
        except LookupError:
            pass


def integration_tests_add_to_deps(collection, dep_collection):
    global integration_tests_deps
    integration_tests_deps.add(dep_collection)
    logger.debug("Adding " + dep_collection + " as a dep for " + collection)


def _rewrite_yaml_mapping_values(el, namespace, collection, spec):
    for key, value in el.items():
        if isinstance(value, Mapping):
            _rewrite_yaml_mapping(el[key], namespace, collection, spec)
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                if isinstance(item, Mapping):
                    _rewrite_yaml_mapping(el[key][idx], namespace, collection, spec)
                else:
                    if key == 'module_blacklist':
                        for coll in spec.keys():
                            if item in get_plugins_from_collection(coll, 'modules', spec):
                                el[key][idx] = get_plugin_fqcn(namespace, coll, el[key][idx])
                                if collection != coll:
                                    integration_tests_add_to_deps(collection, coll)

                    el[key][idx] = _rewrite_yaml_lookup(el[key][idx], namespace, collection, spec)
                    el[key][idx] = _rewrite_yaml_filter(el[key][idx], namespace, collection, spec)
                    el[key][idx] = _rewrite_yaml_test(el[key][idx], namespace, collection, spec)
        elif isinstance(value, str):
            el[key] = _rewrite_yaml_lookup(el[key], namespace, collection, spec)
            el[key] = _rewrite_yaml_filter(el[key], namespace, collection, spec)
            el[key] = _rewrite_yaml_test(el[key], namespace, collection, spec)


def _rewrite_yaml_lookup(value, namespace, collection, spec):
    if not ('lookup(' in value or 'query(' in value or 'q(' in value):
        return value

    for coll in spec.keys():
        for plugin_name in get_plugins_from_collection(coll, 'lookup', spec):
            if plugin_name in value:
                value = value.replace(plugin_name, get_plugin_fqcn(namespace, coll, plugin_name))
                if collection != coll:
                    integration_tests_add_to_deps(collection, coll)

    return value


def _rewrite_yaml_filter(value, namespace, collection, spec):
    if '|' not in value:
        return value

    for coll in spec.keys():
        for filter_plugin_name in get_plugins_from_collection(coll, 'filter', spec):
            imported_module = import_module('ansible.plugins.filter.' + filter_plugin_name)
            fm = getattr(imported_module, 'FilterModule', None)
            if fm is None:
                continue
            filters = fm().filters().keys()
            for found_filter in [match[5] for match in FILTER_RE.findall(value)]:
                if found_filter in filters:
                    value = value.replace(found_filter, get_plugin_fqcn(namespace, coll, found_filter))
                    if collection != coll:
                        integration_tests_add_to_deps(collection, coll)
    return value


def _rewrite_yaml_test(value, namespace, collection, spec):
    if ' is ' not in value:
        return value

    for coll in spec.keys():
        for test_plugin_name in get_plugins_from_collection(coll, 'test', spec):
            imported_module = import_module('ansible.plugins.test.' + test_plugin_name)
            tm = getattr(imported_module, 'TestModule', None)
            if tm is None:
                continue
            tests = tm().tests().keys()
            for found_test in (match[5] for match in TEST_RE.findall(value)):
                if found_test in tests:
                    value = value.replace(found_test, get_plugin_fqcn(namespace, coll, found_test))
                    if collection != coll:
                        integration_tests_add_to_deps(collection, coll)
    return value


##############################################################################
# Rewrite integration tests END
##############################################################################


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-s', '--spec', '--spec_file', required=True, dest='spec_file',
                        help='spec YAML file that describes how to organize collections')
    parser.add_argument('-n', '--ns', '--namespace', dest='namespace', default=COLLECTION_NAMESPACE,
                        help='target namespace for resulting collections')
    parser.add_argument('-r', '--refresh', action='store_true', dest='refresh', default=False,
                        help='force refreshing local Ansible checkout')
    parser.add_argument('-t', '--target-dir', dest='vardir', default=VARDIR,
                        help='target directory for resulting collections and rpm')
    parser.add_argument('-p', '--preserve-module-subdirs', action='store_true', dest='preserve_module_subdirs', default=False,
                        help='preserve module subdirs per spec')

    args = parser.parse_args()

    # required, so we should always have
    spec = load_spec_file(args.spec_file)

    checkout_repo(args.vardir, args.refresh)

    # doeet
    assemble_collections(spec, args)

    global core
    print('======= Assumed stayed in core =======\n')
    print(yaml.dump(core))

if __name__ == "__main__":
    main()
