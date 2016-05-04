import binascii
import copy
import hashlib
import json
import os.path
import shutil
import tempfile
from os import mkdir
from os.path import exists
from subprocess import CalledProcessError, check_call, check_output

import pkgpanda.build.constants
import pkgpanda.build.src_fetchers
from pkgpanda import expand_require as expand_require_exceptions
from pkgpanda import Install, PackageId, Repository
from pkgpanda.cli import add_to_repository
from pkgpanda.constants import RESERVED_UNIT_NAMES
from pkgpanda.exceptions import FetchError, PackageError, ValidationError
from pkgpanda.util import (check_forbidden_services, download_atomic, load_json,
                           load_string, make_file, make_tar, rewrite_symlinks,
                           write_json, write_string)


class BuildError(Exception):
    """An error while building something."""
    def __init__(self, msg):
        assert isinstance(msg, str)
        self.msg = msg

    def __str__(self):
        return self.msg


class DockerCmd:

    def __init__(self):
        self.volumes = dict()
        self.environment = dict()
        self.container = str()

    def run(self, cmd):
        docker = ["docker", "run"]
        for host_path, container_path in self.volumes.items():
            docker += ["-v", "{0}:{1}".format(host_path, container_path)]

        for k, v in self.environment.items():
            docker += ["-e", "{0}={1}".format(k, v)]

        docker.append(self.container)
        docker += cmd
        check_call(docker)


def get_variants_from_filesystem(directory, extension):
    results = set()
    for filename in os.listdir(directory):
        # Skip things that don't end in the extension
        if not filename.endswith(extension):
            continue

        variant = filename[:-len(extension)]

        # Empty name variant shouldn't have a `.` following it
        if variant == '.':
            raise BuildError("Invalid filename {}. The \"default\" variant file should be just {}".format(
                filename, extension))

        # Empty / default variant is represented as 'None'.
        if variant == '':
            variant = None
        else:
            # Should be foo. since we've moved the extension.
            if variant[-1] != '.':
                raise BuildError("Invalid variant filename {}. Expected a '.' separating the "
                                 "variant name and extension '{}'.".format(filename, extension))
            variant = variant[:-1]

        results.add(variant)

    return results


class PackageStore:

    def __init__(self, packages_dir):
        self._packages_dir = packages_dir.rstrip('/')

        # Load all possible packages, making a dictionary from (name, variant) -> buildinfo
        self._packages = dict()
        self._packages_by_name = dict()

        # Iterate through the packages directory finding all packages.
        for name in os.listdir(self._packages_dir):
            package_folder = self.get_package_folder(name)

            # Ignore files / non-directories
            if not os.path.isdir(package_folder):
                continue

            # Search the directory for buildinfo.json files, record the variants
            self._packages_by_name[name] = dict()
            for variant in get_variants_from_filesystem(package_folder, 'buildinfo.json'):
                buildinfo = load_buildinfo(package_folder, variant)
                self._packages[(name, variant)] = buildinfo
                self._packages_by_name[name][variant] = buildinfo

            # If there weren't any packages marked by buildinfo.json files, don't leave the index
            # entry to simplify other code from having to check for empty dictionaries.
            if len(self._packages_by_name[name]) == 0:
                del self._packages_by_name[name]

    def get_package_folder(self, name):
        return self._packages_dir + '/' + name

    def get_buildinfo(self, name, variant):
        return self._packages[(name, variant)]

    def get_last_build_filename(self, name, variant):
        return self.get_package_folder(name) + '/' + last_build_filename(variant)

    def get_treeinfo(self, variant):
        return load_config_variant(self._packages_dir, variant, 'treeinfo.json')

    def list_trees(self):
        return get_variants_from_filesystem(self._packages_dir, 'treeinfo.json')

    @property
    def packages(self):
        return self._packages

    @property
    def packages_by_name(self):
        return self._packages_by_name

    @property
    def packages_dir(self):
        return self._packages_dir


def expand_require(require):
    try:
        return expand_require_exceptions(require)
    except ValidationError as ex:
        raise BuildError(str(ex)) from ex


def get_docker_id(docker_name):
    return check_output(["docker", "inspect", "-f", "{{ .Id }}", docker_name]).decode('utf-8').strip()


def clean(package_dir):
    # Run a docker container to remove src/ and result/
    cmd = DockerCmd()
    cmd.volumes = {
        package_dir: "/pkg/:rw",
    }
    cmd.container = "ubuntu:14.04.4"
    cmd.run(["rm", "-rf", "/pkg/src", "/pkg/result"])


def hash_checkout(item):
    def hash_str(s):
        hasher = hashlib.sha1()
        hasher.update(s.encode('utf-8'))
        return binascii.hexlify(hasher.digest()).decode('ascii')

    def hash_int(i):
        return hash_str(str(i))

    def hash_dict(d):
        item_hashes = []
        for k in sorted(d.keys()):
            assert isinstance(k, str)
            item_hashes.append("{0}={1}".format(k, hash_checkout(d[k])))
        return hash_str(",".join(item_hashes))

    def hash_list(l):
        item_hashes = []
        for item in sorted(l):
            assert isinstance(item, str)
            item_hashes.append(hash_checkout(item))
        return hash_str(",".join(item_hashes))

    if isinstance(item, str) or isinstance(item, bytes):
        return hash_str(item)
    elif isinstance(item, dict):
        return hash_dict(item)
    elif isinstance(item, list):
        return hash_list(item)
    elif isinstance(item, int):
        return hash_int(item)
    else:
        raise NotImplementedError("{} of type {}".format(item, type(item)))


def hash_folder(directory):
    return check_output([
        "/bin/bash",
        "-o", "nounset",
        "-o", "pipefail",
        "-o", "errexit",
        "-c",
        "find {} -type f -print0 | sort -z | xargs -0 sha1sum | sha1sum | cut -d ' ' -f 1".format(
            directory)]).decode('ascii').strip()


def get_last_bootstrap_set(path):
    assert path[-1] != '/'

    def get_last_bootstrap(variant):
        bootstrap_latest = path + '/' + pkgpanda.util.variant_prefix(variant) + 'bootstrap.latest'
        if not os.path.exists(bootstrap_latest):
            raise BuildError("No last bootstrap found for variant {}. Expected to find {} to match "
                             "{}".format(pkgpanda.util.variant_name(variant), bootstrap_latest,
                                         pkgpanda.util.variant_prefix(variant) + 'treeinfo.json'))
        return load_string(bootstrap_latest)

    result = {}
    for variant in PackageStore(path).list_trees():
        result[variant] = get_last_bootstrap(variant)
    return result


def last_build_filename(variant):
    return "cache/" + ((variant + '.') if variant else "") + "latest"


# Try to read json from the given file. If it is an empty file, then return an
# empty json dictionary.
def load_optional_json(filename):
    try:
        with open(filename) as f:
            text = f.read().strip()
            if text:
                return json.loads(text)
            return {}
        return load_json(filename)
    except FileNotFoundError:
        raise BuildError("Didn't find expected JSON file: {}".format(filename))
    except ValueError as ex:
        raise BuildError("Unable to parse json: {}".format(ex))


def load_config_variant(directory, variant, extension):
    assert directory[-1] != '/'
    return load_optional_json(directory + '/' + pkgpanda.util.variant_prefix(variant) + extension)


def load_buildinfo(path, variant):
    buildinfo = load_config_variant(path, variant, 'buildinfo.json')

    # Fill in default / guaranteed members so code everywhere doesn't have to guard around it.
    buildinfo.setdefault('build_script', 'build')
    buildinfo.setdefault('docker', 'dcos-builder:latest')
    buildinfo.setdefault('environment', dict())
    buildinfo.setdefault('requires', list())

    return buildinfo


def make_bootstrap_tarball(packages_dir, packages, variant, repository_url):
    # Convert filenames to package ids
    pkg_ids = list()
    for pkg_path in packages:
        # Get the package id from the given package path
        filename = os.path.basename(pkg_path)
        if not filename.endswith(".tar.xz"):
            raise BuildError("Packages must be packaged / end with a .tar.xz. Got {}".format(filename))
        pkg_id = filename[:-len(".tar.xz")]
        pkg_ids.append(pkg_id)

    # Filename is output_name.<sha-1>.{active.json|.bootstrap.tar.xz}
    bootstrap_id = hash_checkout(pkg_ids)
    latest_name = "{}/{}bootstrap.latest".format(packages_dir, pkgpanda.util.variant_prefix(variant))

    output_name = packages_dir + '/' + bootstrap_id + '.'

    # bootstrap tarball = <sha1 of packages in tarball>.bootstrap.tar.xz
    bootstrap_name = "{}bootstrap.tar.xz".format(output_name)
    active_name = "{}active.json".format(output_name)

    def mark_latest():
        # Ensure latest is always written
        write_string(latest_name, bootstrap_id)

        print("bootstrap: {}".format(bootstrap_name))
        print("active: {}".format(active_name))
        print("latest: {}".format(latest_name))
        return bootstrap_id

    if (os.path.exists(bootstrap_name)):
        print("Bootstrap already up to date, not recreating")
        return mark_latest()

    # Try downloading.
    if repository_url:
        try:
            repository_url = repository_url.rstrip('/')
            bootstrap_url = repository_url + '/bootstrap/{}.bootstrap.tar.xz'.format(bootstrap_id)
            active_url = repository_url + '/bootstrap/{}.active.json'.format(bootstrap_id)
            print("Attempting to download", bootstrap_name, "from", bootstrap_url)
            # Normalize to no trailing slash for repository_url
            download_atomic(bootstrap_name, bootstrap_url, packages_dir)
            print("Attempting to download", active_name, "from", active_url)
            download_atomic(active_name, active_url, packages_dir)

            print("Bootstrap already up to date, Not recreating. Downloaded from repository-url.")
            return mark_latest()
        except FetchError:
            # Fall out and do the build since the command errored.
            print("Unable to download from cache. Building.")

    print("Creating bootstrap tarball for variant {}".format(variant))

    work_dir = tempfile.mkdtemp(prefix='mkpanda_bootstrap_tmp')

    def make_abs(path):
        return os.path.join(work_dir, path)

    pkgpanda_root = make_abs("opt/mesosphere")
    repository = Repository(os.path.join(pkgpanda_root, "packages"))

    # Fetch all the packages to the root
    for pkg_path in packages:
        filename = os.path.basename(pkg_path)
        pkg_id = filename[:-len(".tar.xz")]

        def local_fetcher(id, target):
            shutil.unpack_archive(pkg_path, target, "gztar")
        repository.add(local_fetcher, pkg_id, False)

    # Activate the packages inside the repository.
    # Do generate dcos.target.wants inside the root so that we don't
    # try messing with /etc/systemd/system.
    install = Install(pkgpanda_root, None, True, False, True, True, True)
    install.activate(repository.load_packages(pkg_ids))

    # Mark the tarball as a bootstrap tarball/filesystem so that
    # dcos-setup.service will fire.
    make_file(make_abs("opt/mesosphere/bootstrap"))

    # Write out an active.json for the bootstrap tarball
    write_json(active_name, pkg_ids)

    # Rewrite all the symlinks to point to /opt/mesosphere
    rewrite_symlinks(work_dir, work_dir, "/")

    make_tar(bootstrap_name, pkgpanda_root)

    shutil.rmtree(work_dir)

    # Update latest last so that we don't ever use partially-built things.
    write_string(latest_name, bootstrap_id)

    print("Built bootstrap")
    return mark_latest()


ALLOWED_TREEINFO_KEYS = {'exclude', 'variants', 'core_package_list'}


def get_tree_package_tuples(package_store, tree_variant):
    treeinfo = package_store.get_treeinfo(tree_variant)

    if treeinfo.keys() > ALLOWED_TREEINFO_KEYS:
        raise BuildError(
            "treeinfo can only include the keys {}. Found {}".format(ALLOWED_TREEINFO_KEYS, treeinfo.keys()))

    core_package_list = treeinfo.get('core_package_list', None)
    if core_package_list is not None and not isinstance(core_package_list, list):
        raise BuildError(
            "core_package_list must either be null meaning don't use or a list of the core "
            "packages to include (dependencies are automatically picked up).")

    excludes = treeinfo.get('exclude', list())
    if not isinstance(excludes, list):
        raise BuildError("treeinfo exclude must be a list of packages to exclude.")

    for exclude in excludes:
        if not isinstance(exclude, str):
            raise BuildError("Excludes should be a list of strings of package names. Found a {} "
                             "with the value: {}".format(type(exclude), exclude))

    # Validate core_package_lists is formatted as expected, doesn't contain
    # any of exclude.
    if core_package_list is not None:
        for name in core_package_list:
            if not isinstance(name, str):
                raise BuildError("core_package_list should be a list of package name strings, found "
                                 "a {} with the value: {}".format(type(name), name))

            if name in excludes:
                raise BuildError("Package found in both exclude and core_package_list: {}".format(name))

    # List of mandatory package variants to include in the buildinfo.
    variants = treeinfo.get('variants', dict())

    if not isinstance(variants, dict):
        raise BuildError("treeinfo variants must be a dictionary of package name to variant name")

    # Generate the list of package paths of all packages variants which were
    # included and excluding those removed.
    package_names = set()
    package_tuples = set()

    def include_package(name, variant):
        if name in excludes:
            raise BuildError("package {} is in excludes but was needed as a dependency of an "
                             "included package".format(name))

        if (name, variant) not in package_store.packages:
            raise BuildError("package {} variant {} is needed but is not in the set of built "
                             "packages but is needed (explicitly requested or as a requires)".format(name, variant))

        # Allow adding duplicates. There is a check that we don't have a repeat
        # of the same package name with different variants, so we can ignore the
        # variant name.
        if name in package_names:
            pass
        package_names.add(name)
        package_tuples.add((name, variant))

    for name in package_store.packages_by_name.keys():
        if core_package_list is not None:
            assert isinstance(core_package_list, list)

            # Skip over packages not in the core package list. We'll add requires
            # later when resolving / validating the requires graph.
            if name not in core_package_list:
                continue

        if name in excludes:
            continue

        # Sanity check
        assert name not in package_names

        include_package(name, variants.get(name))

    # Validate that all mandatory package variants are included
    for name, variant in variants.items():
        if (name, variant) not in package_tuples:
            raise BuildError("package {} is supposed to have variant {} included in "
                             "the tree according to the treeinfo.json, but the no such package "
                             "(let alone variant) was found".format(name, variant))

    # Validate that all required packages are included. This implicitly
    # validates that no two packages include conflicting variants. If they
    # included different variants, only one of the variant could be included
    # because we iterate over the list of packages once so only one variant
    # could get included. If another variant is asked for in the requires,
    # then that variant won't be included and we'll error.
    to_visit = list(package_tuples)
    while len(to_visit) > 0:
        name, variant = to_visit.pop()
        requires = package_store.get_buildinfo(name, variant)['requires']
        for require in requires:
            require_tuple = expand_require(require)
            if require_tuple not in package_tuples:
                if core_package_list is not None:
                    # TODO(cmaloney): Include the context information of the
                    # else case when printing out the info.
                    include_package(require_tuple[0], require_tuple[1])
                    to_visit.append(require_tuple)
                else:
                    raise BuildError("Package {} requires {} variant {} but that is not in the set "
                                     "of packages listed for the tree {}: {}".format(
                                        name,
                                        require_tuple[0],
                                        require_tuple[1],
                                        tree_variant,
                                        package_tuples))

    # Integrity / programming check excludes were all excluded.
    for exclude in excludes:
        assert exclude not in package_names
    return package_tuples


def build_tree(packages_dir, mkbootstrap, repository_url, tree_variant):
    package_store = PackageStore(packages_dir)

    # Check the requires and figure out a feasible build order
    # depth-first traverse the dependency tree, yielding when we reach a
    # leaf or all the dependencies of package have been built. If we get
    # back to a node without all it's dependencies built, error (likely
    # circular).
    # TODO(cmaloney): Add support for circular dependencies. They are doable
    # long as there is a pre-built version of enough of the packages.

    # TODO(cmaloney): Make it so when we're building a treeinfo which has a
    # explicit package list we don't build all the other packages.
    build_order = list()
    visited = set()
    built = set()

    def visit(pkg_tuple):
        # Visit the node for the first (and only time). Finding a node again
        # means a cycle and should be detected at caller.

        assert isinstance(pkg_tuple, tuple)

        assert pkg_tuple not in visited
        visited.add(pkg_tuple)

        # Ensure all dependencies are built. Sorted for stability
        for require in sorted(package_store.packages[pkg_tuple]['requires']):
            require_tuple = expand_require(require)
            if require_tuple in built:
                continue
            if require_tuple in visited:
                raise BuildError("Circular dependency. Circular link {0} -> {1}".format(pkg_tuple, require_tuple))

            if PackageId.is_id(require_tuple[0]):
                raise BuildError("Depending on a specific package id is not supported. Package {} "
                                 "depends on {}".format(pkg_tuple, require_tuple))

            if require_tuple not in package_store.packages:
                raise BuildError("Package {0} require {1} not buildable from tree.".format(pkg_tuple, require_tuple))

            visit(require_tuple)

        build_order.append(pkg_tuple)
        built.add(pkg_tuple)

    # Can't compare none to string, so expand none -> "true" / "false", then put
    # the string in a field after "" if none, the string if not.
    def key_func(elem):
        return elem[0], elem[1] is None, elem[1] or ""

    def visit_packages_in_tree(variant):
        all_tuples = get_tree_package_tuples(package_store, variant)
        for pkg_tuple in sorted(all_tuples, key=key_func):
            if pkg_tuple in visited:
                continue
            visit(pkg_tuple)

    if tree_variant is None:
        tree_visit_list = list(sorted(package_store.list_trees(), key=pkgpanda.util.variant_str))
    else:
        tree_visit_list = [tree_variant]

    for tree in tree_visit_list:
        visit_packages_in_tree(tree)

    built_packages = dict()
    for (name, variant) in build_order:
        print("Building: {} variant {}".format(name, pkgpanda.util.variant_str(variant)))
        built_packages.setdefault(name, dict())

        # Run the build, store the built package path for later use.
        # TODO(cmaloney): Only build the requested variants, rather than all variants.
        built_packages[name][variant] = build(
            package_store,
            name,
            variant,
            repository_url,
            True)

    def make_bootstrap(variant):
        print("Making bootstrap variant:", variant or "<default>")
        package_paths = list()
        for name, pkg_variant in get_tree_package_tuples(package_store, variant):
            package_paths.append(built_packages[name][pkg_variant])

        if mkbootstrap:
            return make_bootstrap_tarball(packages_dir, list(sorted(package_paths)), variant, repository_url)

    # Make sure all treeinfos are satisfied and generate their bootstrap
    # tarballs if requested.
    # TODO(cmaloney): Allow distinguishing between "build all" and "build the default one".
    results = {}
    for variant in tree_visit_list:
        results[variant] = make_bootstrap(variant)

    return results


def expand_single_source_alias(pkg_name, buildinfo):
    if "sources" in buildinfo:
        return buildinfo["sources"]
    elif "single_source" in buildinfo:
        return {pkg_name: buildinfo["single_source"]}
    else:
        print("NOTICE: No sources specified")
        return {}


def assert_no_duplicate_keys(lhs, rhs):
    if len(lhs.keys() & rhs.keys()) != 0:
        print("ASSERTION FAILED: Duplicate keys between {} and {}".format(lhs, rhs))
        assert len(lhs.keys() & rhs.keys()) == 0


# Find all build variants and build them
def build_package_variants(package_store, name, repository_url, clean_after_build=True):
    # Find the packages dir / root of the packages tree, and create a PackageStore
    results = dict()
    for variant in package_store.packages_by_name[name].keys():
        results[variant] = build(
            package_store,
            name,
            variant,
            repository_url=repository_url,
            clean_after_build=clean_after_build)
    return results


def build(package_store, name, variant, repository_url, clean_after_build):
    assert isinstance(package_store, PackageStore)
    print("Building package {} variant {}".format(name, pkgpanda.util.variant_str(variant)))
    tmpdir = tempfile.TemporaryDirectory(prefix="pkgpanda_repo")
    repository = Repository(tmpdir.name)

    package_dir = package_store.get_package_folder(name)

    def pkg_abs(name):
        return package_dir + '/' + name

    # Build pkginfo over time, translating fields from buildinfo.
    pkginfo = {}

    # Build up the docker command arguments over time, translating fields as needed.
    cmd = DockerCmd()

    assert (name, variant) in package_store.packages, \
        "Programming error: name, variant should have been validated to be valid before calling build()."
    buildinfo = copy.deepcopy(package_store.get_buildinfo(name, variant))

    if 'name' in buildinfo:
        raise BuildError("'name' is not allowed in buildinfo.json, it is implicitly the name of the "
                         "folder containing the buildinfo.json")

    # Convert single_source -> sources
    try:
        sources = expand_single_source_alias(name, buildinfo)
    except ValidationError as ex:
        raise BuildError("Invalid buildinfo.json for package: {}".format(ex)) from ex

    # Save the final sources back into buildinfo so it gets written into
    # buildinfo.json. This also means buildinfo.json is always expanded form.
    buildinfo['sources'] = sources

    # Construct the source fetchers, gather the checkout ids from them
    checkout_ids = dict()
    fetchers = dict()
    try:
        for src_name, src_info in sorted(sources.items()):
            if src_info['kind'] not in pkgpanda.build.src_fetchers.all_fetchers:
                raise ValidationError("No known way to catch src with kind '{}'. Known kinds: {}".format(
                    src_info['kind'],
                    pkgpanda.src_fetchers.all_fetchers.keys()))

            cache_dir = pkg_abs("cache")
            if not os.path.exists(cache_dir):
                os.mkdir(cache_dir)

            fetchers[src_name] = pkgpanda.build.src_fetchers.all_fetchers[src_info['kind']](src_name,
                                                                                            src_info,
                                                                                            package_dir)
            checkout_ids[src_name] = fetchers[src_name].get_id()
    except ValidationError as ex:
        raise BuildError("Validation error when fetching sources for package: {}".format(ex))

    for src_name, checkout_id in checkout_ids.items():
        # NOTE: single_source buildinfo was expanded above so the src_name is
        # always correct here.
        # Make sure we never accidentally overwrite something which might be
        # important. Fields should match if specified (And that should be
        # tested at some point). For now disallowing identical saves hassle.
        assert_no_duplicate_keys(checkout_id, buildinfo['sources'][src_name])
        buildinfo['sources'][src_name].update(checkout_id)

    # Add the sha1sum of the buildinfo.json + build file to the build ids
    build_ids = {"sources": checkout_ids}
    build_ids['build'] = pkgpanda.util.sha1(pkg_abs(buildinfo['build_script']))
    build_ids['pkgpanda_version'] = pkgpanda.build.constants.version
    build_ids['variant'] = '' if variant is None else variant

    extra_dir = pkg_abs("extra")
    # Add the "extra" folder inside the package as an additional source if it
    # exists
    if os.path.exists(extra_dir):
        extra_id = hash_folder(extra_dir)
        build_ids['extra_source'] = extra_id
        buildinfo['extra_source'] = extra_id

    # Figure out the docker name.
    docker_name = buildinfo['docker']
    cmd.container = docker_name

    # Add the id of the docker build environment to the build_ids.
    try:
        docker_id = get_docker_id(docker_name)
    except CalledProcessError:
        # docker pull the container and try again
        check_call(['docker', 'pull', docker_name])
        docker_id = get_docker_id(docker_name)

    build_ids['docker'] = docker_id

    # TODO(cmaloney): The environment variables should be generated during build
    # not live in buildinfo.json.
    build_ids['environment'] = buildinfo['environment']

    # Packages need directories inside the fake install root (otherwise docker
    # will try making the directories on a readonly filesystem), so build the
    # install root now, and make the package directories in it as we go.
    install_dir = tempfile.mkdtemp(prefix="pkgpanda-")

    active_packages = list()
    active_package_ids = set()
    active_package_variants = dict()
    auto_deps = set()
    # Verify all requires are in the repository.
    if 'requires' in buildinfo:
        # Final package has the same requires as the build.
        pkginfo['requires'] = buildinfo['requires']

        # TODO(cmaloney): Pull generating the full set of requires a function.
        to_check = copy.deepcopy(buildinfo['requires'])
        if type(to_check) != list:
            raise BuildError("`requires` in buildinfo.json must be an array of dependencies.")
        while to_check:
            requires_info = to_check.pop(0)
            requires_name, requires_variant = expand_require(requires_info)

            if requires_name in active_package_variants:
                # TODO(cmaloney): If one package depends on the <default>
                # variant of a package and 1+ others depends on a non-<default>
                # variant then update the dependency to the non-default variant
                # rather than erroring.
                if requires_variant != active_package_variants[requires_name]:
                    # TODO(cmaloney): Make this contain the chains of
                    # dependencies which contain the conflicting packages.
                    # a -> b -> c -> d {foo}
                    # e {bar} -> d {baz}
                    raise BuildError("Dependncy on multiple variants of the same package {}. "
                                     "variants: {} {}".format(
                                        requires_name,
                                        requires_variant,
                                        active_package_variants[requires_name]))

                # The variant has package {requires_name, variant} already is a
                # dependency, don't process it again / move on to the next.
                continue

            active_package_variants[requires_name] = requires_variant

            # Figure out the last build of the dependency, add that as the
            # fully expanded dependency.
            requires_last_build = package_store.get_last_build_filename(requires_name, requires_variant)
            if not os.path.exists(requires_last_build):
                raise BuildError("No last build file found for dependency {} variant {}. Rebuild "
                                 "the dependency".format(requires_name, requires_variant))

            try:
                pkg_id_str = load_string(requires_last_build)
                auto_deps.add(pkg_id_str)
                pkg_buildinfo = package_store.get_buildinfo(requires_name, requires_variant)
                pkg_requires = pkg_buildinfo['requires']
                pkg_path = repository.package_path(pkg_id_str)
                pkg_tar = pkg_id_str + '.tar.xz'
                if not os.path.exists(package_store.get_package_folder(requires_name) + '/' + pkg_tar):
                    raise BuildError("The build tarball {} refered to by the last_build file of the "
                                     "dependency {} variant {} doesn't exist. Rebuild the dependency.".format(
                                        pkg_tar,
                                        requires_name,
                                        requires_variant))

                active_package_ids.add(pkg_id_str)

                # Mount the package into the docker container.
                cmd.volumes[pkg_path] = "/opt/mesosphere/packages/{}:ro".format(pkg_id_str)
                os.makedirs(os.path.join(install_dir, "packages/{}".format(pkg_id_str)))

                # Add the dependencies of the package to the set which will be
                # activated.
                # TODO(cmaloney): All these 'transitive' dependencies shouldn't
                # be available to the package being built, only what depends on
                # them directly.
                to_check += pkg_requires
            except ValidationError as ex:
                raise BuildError("validating package needed as dependency {0}: {1}".format(requires_name, ex)) from ex
            except PackageError as ex:
                raise BuildError("loading package needed as dependency {0}: {1}".format(requires_name, ex)) from ex

    # Add requires to the package id, calculate the final package id.
    # NOTE: active_packages isn't fully constructed here since we lazily load
    # packages not already in the repository.
    build_ids['requires'] = list(active_package_ids)
    version_base = hash_checkout(build_ids)
    version = None
    if "version_extra" in buildinfo:
        version = "{0}-{1}".format(buildinfo["version_extra"], version_base)
    else:
        version = version_base
    pkg_id = PackageId.from_parts(name, version)

    # Save the build_ids. Useful for verify exactly what went into the
    # package build hash.
    buildinfo['build_ids'] = build_ids
    buildinfo['package_version'] = version

    # Save the package name and variant. The variant is used when installing
    # packages to validate dependencies.
    buildinfo['name'] = name
    buildinfo['variant'] = variant

    # If the package is already built, don't do anything.
    pkg_path = pkg_abs("{}.tar.xz".format(pkg_id))

    # Done if it exists locally
    if exists(pkg_path):
        print("Package up to date. Not re-building.")

        # TODO(cmaloney): Updating / filling last_build should be moved out of
        # the build function.
        check_call(["mkdir", "-p", pkg_abs("cache")])
        write_string(pkg_abs(last_build_filename(variant)), str(pkg_id))

        return pkg_path

    # Try downloading.
    if repository_url:
        try:
            # Normalize to no trailing slash for repository_url
            repository_url = repository_url.rstrip('/')
            url = repository_url + '/packages/{0}/{1}.tar.xz'.format(pkg_id.name, str(pkg_id))
            print("Attempting to download", pkg_id, "from", url)
            download_atomic(pkg_path, url, package_dir)

            print("Package up to date. Not re-building. Downloaded from repository-url.")
            # TODO(cmaloney): Updating / filling last_build should be moved out of
            # the build function.
            check_call(["mkdir", "-p", pkg_abs("cache")])
            write_string(pkg_abs(last_build_filename(variant)), str(pkg_id))
            return pkg_path
        except FetchError:
            # Fall out and do the build since the command errored.
            print("Unable to download from cache. Proceeding to build")

    print("Building package {} with buildinfo: {}".format(
        pkg_id,
        json.dumps(buildinfo, indent=2, sort_keys=True)))

    # Clean out src, result so later steps can use them freely for building.
    clean(package_dir)

    # Only fresh builds are allowed which don't overlap existing artifacts.
    result_dir = pkg_abs("result")
    if exists(result_dir):
        raise BuildError("result folder must not exist. It will be made when the package is "
                         "built. {}".format(result_dir))

    # 'mkpanda add' all implicit dependencies since we actually need to build.
    for dep in auto_deps:
        print("Auto-adding dependency: {}".format(dep))
        # NOTE: Not using the name pkg_id because that overrides the outer one.
        id_obj = PackageId(dep)
        add_to_repository(repository, pkg_abs('../{0}/{1}.tar.xz'.format(id_obj.name, dep)))
        package = repository.load(dep)
        active_packages.append(package)

    # Checkout all the sources int their respective 'src/' folders.
    try:
        src_dir = pkg_abs('src')
        if os.path.exists(src_dir):
            raise ValidationError(
                "'src' directory already exists, did you have a previous build? " +
                "Currently all builds must be from scratch. Support should be " +
                "added for re-using a src directory when possible. src={}".format(src_dir))
        os.mkdir(src_dir)
        for src_name, fetcher in sorted(fetchers.items()):
            root = pkg_abs('src/' + src_name)
            os.mkdir(root)

            fetcher.checkout_to(root)
    except ValidationError as ex:
        raise BuildError("Validation error when fetching sources for package: {}".format(ex))

    # Copy over environment settings
    pkginfo['environment'] = buildinfo['environment']

    # Activate the packages so that we have a proper path, environment
    # variables.
    # TODO(cmaloney): RAII type thing for temproary directory so if we
    # don't get all the way through things will be cleaned up?
    install = Install(install_dir, None, True, False, True, True)
    install.activate(active_packages)
    # Rewrite all the symlinks inside the active path because we will
    # be mounting the folder into a docker container, and the absolute
    # paths to the packages will change.
    # TODO(cmaloney): This isn't very clean, it would be much nicer to
    # just run pkgpanda inside the package.
    rewrite_symlinks(install_dir, repository.path, "/opt/mesosphere/packages/")

    print("Building package in docker")

    # TODO(cmaloney): Run as a specific non-root user, make it possible
    # for non-root to cleanup afterwards.
    # Run the build, prepping the environment as necessary.
    mkdir(pkg_abs("result"))

    # Copy the build info to the resulting tarball
    write_json(pkg_abs("src/buildinfo.full.json"), buildinfo)
    write_json(pkg_abs("result/buildinfo.full.json"), buildinfo)

    write_json(pkg_abs("result/pkginfo.json"), pkginfo)

    # Make the folder for the package we are building. If docker does it, it
    # gets auto-created with root permissions and we can't actually delete it.
    os.makedirs(os.path.join(install_dir, "packages", str(pkg_id)))

    # TOOD(cmaloney): Disallow writing to well known files and directories?
    # Source we checked out
    cmd.volumes.update({
        # TODO(cmaloney): src should be read only...
        pkg_abs("src"): "/pkg/src:rw",
        # The build script
        pkg_abs(buildinfo['build_script']): "/pkg/build:ro",
        # Getting the result out
        pkg_abs("result"): "/opt/mesosphere/packages/{}:rw".format(pkg_id),
        install_dir: "/opt/mesosphere:ro"
    })

    if os.path.exists(extra_dir):
        cmd.volumes[extra_dir] = "/pkg/extra:ro"

    cmd.environment = {
        "PKG_VERSION": version,
        "PKG_NAME": name,
        "PKG_ID": pkg_id,
        "PKG_PATH": "/opt/mesosphere/packages/{}".format(pkg_id),
        "PKG_VARIANT": variant if variant is not None else "<default>"
    }

    try:
        # TODO(cmaloney): Run a wrapper which sources
        # /opt/mesosphere/environment then runs a build. Also should fix
        # ownership of /opt/mesosphere/packages/{pkg_id} post build.
        cmd.run([
            "/bin/bash",
            "-o", "nounset",
            "-o", "pipefail",
            "-o", "errexit",
            "/pkg/build"])
    except CalledProcessError as ex:
        raise BuildError("docker exited non-zero: {}\nCommand: {}".format(ex.returncode, ' '.join(ex.cmd)))

    # Clean up the temporary install dir used for dependencies.
    # TODO(cmaloney): Move to an RAII wrapper.
    check_call(['rm', '-rf', install_dir])

    print("Building package tarball")

    # Check for forbidden services before packaging the tarball:
    try:
        check_forbidden_services(pkg_abs("result"), RESERVED_UNIT_NAMES)
    except ValidationError as ex:
        raise BuildError("Package validation failed: {}".format(ex))

    # TODO(cmaloney): Updating / filling last_build should be moved out of
    # the build function.
    check_call(["mkdir", "-p", pkg_abs("cache")])
    write_string(pkg_abs(last_build_filename(variant)), str(pkg_id))

    # Bundle the artifacts into the pkgpanda package
    tmp_name = pkg_path + "-tmp.tar.xz"
    make_tar(tmp_name, pkg_abs("result"))
    os.rename(tmp_name, pkg_path)
    print("Package built.")
    if clean_after_build:
        clean(package_dir)
    return pkg_path
