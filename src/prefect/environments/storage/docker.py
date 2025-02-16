import filecmp
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import textwrap
import uuid
import warnings
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, Iterable, List

import cloudpickle
import docker
import pendulum
from slugify import slugify

import prefect
from prefect.environments.storage import Storage


class Docker(Storage):
    """
    Docker storage provides a mechanism for storing Prefect flows in Docker images
    and optionally pushing them to a registry.

    A user specifies a `registry_url`, `base_image` and other optional dependencies (e.g., `python_dependencies`)
    and `build()` will create a temporary Dockerfile that is used to build the image.

    Note that the `base_image` must be capable of `pip` installing.  Note that registry behavior with respect to
    image names can differ between providers - for example, Google's GCR registry allows for registry URLs of the form
    `gcr.io/my-registry/subdir/my-image-name` whereas DockerHub requires the registry URL to be separate from the image name.

    Args:
        - registry_url (str, optional): URL of a registry to push the image to; image will not be pushed if not provided
        - base_image (str, optional): the base image for this environment (e.g. `python:3.6`), defaults to the `prefecthq/prefect` image
            matching your python version and prefect core library version used at runtime.
        - dockerfile (str, optional): a path to a Dockerfile to use in building this storage; note that, if provided,
            your present working directory will be used as the build context
        - python_dependencies (List[str], optional): list of pip installable dependencies for the image
        - image_name (str, optional): name of the image to use when building, populated with a UUID after build
        - image_tag (str, optional): tag of the image to use when building, populated with a UUID after build
        - env_vars (dict, optional): a dictionary of environment variables to use when building
        - files (dict, optional): a dictionary of files to copy into the image when building
        - base_url: (str, optional): a URL of a Docker daemon to use when for Docker related functionality
        - prefect_version (str, optional): an optional branch, tag, or commit specifying the version of prefect
            you want installed into the container; defaults to the version you are currently using or `"master"` if your version is ahead of
            the latest tag
        - local_image(bool, optional): an optional flag whether or not to use a local docker image, if True then a pull will not be attempted

    Raises:
        - ValueError: if both `base_image` and `dockerfile` are provided

    """

    def __init__(
        self,
        registry_url: str = None,
        base_image: str = None,
        dockerfile: str = None,
        python_dependencies: List[str] = None,
        image_name: str = None,
        image_tag: str = None,
        env_vars: dict = None,
        files: dict = None,
        base_url: str = None,
        prefect_version: str = None,
        local_image: bool = False,
    ) -> None:
        self.registry_url = registry_url

        if sys.platform == "win32":
            default_url = "npipe:////./pipe/docker_engine"
        else:
            default_url = "unix://var/run/docker.sock"

        self.image_name = image_name
        self.image_tag = image_tag
        self.python_dependencies = python_dependencies or []
        self.python_dependencies.append("wheel")

        self.env_vars = env_vars or {}
        self.env_vars.setdefault(
            "PREFECT__USER_CONFIG_PATH", "/root/.prefect/config.toml"
        )

        self.files = files or {}
        self.flows = dict()  # type: Dict[str, str]
        self._flows = dict()  # type: Dict[str, "prefect.core.flow.Flow"]
        self.base_url = base_url or default_url
        self.local_image = local_image
        self.extra_commands = []  # type: List[str]

        version = prefect.__version__.split("+")
        if prefect_version is None:
            self.prefect_version = "master" if len(version) > 1 else version[0]
        else:
            self.prefect_version = prefect_version

        if base_image is None and dockerfile is None:
            python_version = "{}.{}".format(
                sys.version_info.major, sys.version_info.minor
            )
            if re.match("^[0-9]+\.[0-9]+\.[0-9]+$", self.prefect_version) != None:
                self.base_image = "prefecthq/prefect:{}-python{}".format(
                    self.prefect_version, python_version
                )
            else:
                # create an image from python:*-slim directly
                self.base_image = "python:{}-slim".format(python_version)
                self.extra_commands.append(
                    "apt update && apt install -y gcc git && rm -rf /var/lib/apt/lists/*",
                )
        elif base_image and dockerfile:
            raise ValueError(
                "Only one of `base_image` and `dockerfile` can be provided."
            )
        else:
            self.base_image = base_image  # type: ignore

        self.dockerfile = dockerfile
        # we should always try to install prefect, unless it is already installed. We can't determine this until
        # image build time.
        self.extra_commands.append(
            "pip show prefect || pip install git+https://github.com/PrefectHQ/prefect.git@{}#egg=prefect[kubernetes]".format(
                self.prefect_version
            ),
        )

        not_absolute = [
            file_path for file_path in self.files if not os.path.isabs(file_path)
        ]
        if not_absolute:
            raise ValueError(
                "Provided paths {} are not absolute file paths, please provide absolute paths only.".format(
                    ", ".join(not_absolute)
                )
            )

    def get_env_runner(self, flow_location: str) -> Callable[[Dict[str, str]], None]:
        """
        Given a flow_location within this Storage object, returns something with a
        `run()` method which accepts the standard runner kwargs and can run the flow.

        Args:
            - flow_location (str): the location of a flow within this Storage

        Returns:
            - a runner interface (something with a `run()` method for running the flow)
        """

        def runner(env: dict) -> None:
            """
            Given a dictionary of environment variables, calls `flow.run()` with these
            environment variables set.
            """
            image = "{}:{}".format(self.image_name, self.image_tag)
            client = docker.APIClient(base_url=self.base_url, version="auto")
            container = client.create_container(image, command="tail -f /dev/null")
            client.start(container=container.get("Id"))
            python_script = "import cloudpickle; f = open('{}', 'rb'); flow = cloudpickle.load(f); f.close(); flow.run()".format(
                flow_location
            )
            try:
                ee = client.exec_create(
                    container.get("Id"),
                    'python -c "{}"'.format(python_script),
                    environment=env,
                )
                output = client.exec_start(exec_id=ee, stream=True)
                for item in output:
                    for line in item.decode("utf-8").split("\n"):
                        if line:
                            print(line)
            finally:
                client.stop(container=container.get("Id"))

        return runner

    def add_flow(self, flow: "prefect.core.flow.Flow") -> str:
        """
        Method for adding a new flow to this Storage object.

        Args:
            - flow (Flow): a Prefect Flow to add

        Returns:
            - str: the location of the newly added flow in this Storage object
        """
        if flow.name in self:
            raise ValueError(
                'Name conflict: Flow with the name "{}" is already present in this storage.'.format(
                    flow.name
                )
            )
        flow_path = "/root/.prefect/flows/{}.prefect".format(slugify(flow.name))
        self.flows[flow.name] = flow_path
        self._flows[flow.name] = flow  # needed prior to build
        return flow_path

    def get_flow(self, flow_location: str) -> "prefect.core.flow.Flow":
        """
        Given a file path within this Docker container, returns the underlying Flow.
        Note that this method should only be run _within_ the container itself.

        Args:
            - flow_location (str): the file path of a flow within this container

        Returns:
            - Flow: the requested flow
        """
        with open(flow_location, "rb") as f:
            return cloudpickle.load(f)

    @property
    def name(self) -> str:
        """
        Full name of the Docker image.
        """
        if None in [self.image_name, self.image_tag]:
            raise ValueError("Docker storage is missing required fields")

        return "{}:{}".format(
            PurePosixPath(self.registry_url or "", self.image_name),  # type: ignore
            self.image_tag,  # type: ignore
        )

    def __contains__(self, obj: Any) -> bool:
        """
        Method for determining whether an object is contained within this storage.
        """
        if not isinstance(obj, str):
            return False
        return obj in self.flows

    def build(self, push: bool = True) -> "Storage":
        """
        Build the Docker storage object.  If image name and tag are not set,
        they will be autogenerated.

        Args:
            - push (bool, optional): Whether or not to push the built Docker image, this
                requires the `registry_url` to be set

        Returns:
            - Docker: a new Docker storage object that contains information about how and
                where the flow is stored. Image name and tag are generated during the
                build process.

        Raises:
            - InterruptedError: if either pushing or pulling the image fails
        """
        if len(self.flows) != 1:
            self.image_name = self.image_name or str(uuid.uuid4())
        else:
            self.image_name = self.image_name or slugify(list(self.flows.keys())[0])

        self.image_tag = self.image_tag or slugify(pendulum.now("utc").isoformat())
        self._build_image(push=push)
        return self

    def _build_image(self, push: bool = True) -> tuple:
        """
        Build a Docker image using the docker python library.

        Args:
            - push (bool, optional): Whether or not to push the built Docker image, this
                requires the `registry_url` to be set

        Returns:
            - tuple: generated UUID strings `image_name`, `image_tag`

        Raises:
            - ValueError: if the image fails to build
            - InterruptedError: if either pushing or pulling the image fails
        """
        assert isinstance(self.image_name, str), "Image name must be provided"
        assert isinstance(self.image_tag, str), "An image tag must be provided"

        # Make temporary directory to hold serialized flow, healthcheck script, and dockerfile
        # note that if the user provides a custom dockerfile, we create the temporary directory
        # within the current working directory to preserve their build context
        with tempfile.TemporaryDirectory(
            dir="." if self.dockerfile else None
        ) as tempdir:

            # Build the dockerfile
            if self.base_image and not self.local_image:
                self.pull_image()

            dockerfile_path = self.create_dockerfile_object(directory=tempdir)
            client = docker.APIClient(base_url=self.base_url, version="auto")

            # Verify that a registry url has been provided for images that should be pushed
            if self.registry_url:
                full_name = str(PurePosixPath(self.registry_url, self.image_name))
            elif push is True:
                warnings.warn(
                    "This Docker storage object has no `registry_url`, and will not be pushed.",
                    UserWarning,
                )
                full_name = self.image_name
            else:
                full_name = self.image_name

            # Use the docker client to build the image
            logging.info("Building the flow's Docker storage...")
            output = client.build(
                path="." if self.dockerfile else tempdir,
                dockerfile=dockerfile_path,
                tag="{}:{}".format(full_name, self.image_tag),
                forcerm=True,
            )
            self._parse_generator_output(output)

            if len(client.images(name=full_name)) == 0:
                raise ValueError(
                    "Your docker image failed to build!  Your flow might have failed one of its deployment health checks - please ensure that all necessary files and dependencies have been included."
                )

            # Push the image if requested
            if push and self.registry_url:
                self.push_image(full_name, self.image_tag)

                # Remove the image locally after being pushed
                client.remove_image(
                    image="{}:{}".format(full_name, self.image_tag), force=True
                )

        return self.image_name, self.image_tag

    ########################
    # Dockerfile Creation
    ########################

    def create_dockerfile_object(self, directory: str) -> str:
        """
        Writes a dockerfile to the provided directory using the specified
        arguments on this Docker storage object.

        In order for the docker python library to build a container it needs a
        Dockerfile that it can use to define the container. This function takes the
        specified arguments then writes them to a temporary file called Dockerfile.

        *Note*: if `files` are added to this container, they will be copied to this directory as well.

        Args:
            - directory (str, optional): A directory where the Dockerfile will be created,
                if no directory is specified is will be created in the current working directory

        Returns:
            - str: the absolute file path to the Dockerfile
        """
        # Generate single pip install command for python dependencies
        pip_installs = "RUN pip install "
        if self.python_dependencies:
            for dependency in self.python_dependencies:
                pip_installs += "{} ".format(dependency)

        # Generate ENV variables to load into the image
        env_vars = ""
        if self.env_vars:
            white_space = " " * 20
            env_vars = "ENV " + " \ \n{}".format(white_space).join(
                "{k}={v}".format(k=k, v=v) for k, v in self.env_vars.items()
            )

        # Copy user specified files into the image
        copy_files = ""
        if self.files:
            for src, dest in self.files.items():
                fname = os.path.basename(src)
                full_fname = os.path.join(directory, fname)
                if os.path.exists(full_fname) and filecmp.cmp(src, full_fname) is False:
                    raise ValueError(
                        "File {fname} already exists in {directory}".format(
                            fname=full_fname, directory=directory
                        )
                    )
                else:
                    shutil.copy2(src, full_fname)
                copy_files += "COPY {fname} {dest}\n".format(
                    fname=full_fname if self.dockerfile else fname, dest=dest
                )

        # Write all flows to file and load into the image
        copy_flows = ""
        for flow_name, flow_location in self.flows.items():
            clean_name = slugify(flow_name)
            flow_path = os.path.join(directory, "{}.flow".format(clean_name))
            with open(flow_path, "wb") as f:
                cloudpickle.dump(self._flows[flow_name], f)
            copy_flows += "COPY {source} {dest}\n".format(
                source=flow_path if self.dockerfile else "{}.flow".format(clean_name),
                dest=flow_location,
            )

        # Write all extra commands that should be run in the image
        extra_commands = ""
        for cmd in self.extra_commands:
            extra_commands += "RUN {}\n".format(cmd)

        # Write a healthcheck script into the image
        with open(
            os.path.join(os.path.dirname(__file__), "_healthcheck.py"), "r"
        ) as healthscript:
            healthcheck = healthscript.read()

        healthcheck_loc = os.path.join(directory, "healthcheck.py")
        with open(healthcheck_loc, "w") as health_file:
            health_file.write(healthcheck)

        if self.dockerfile:
            with open(self.dockerfile, "r") as contents:
                base_commands = textwrap.indent("\n" + contents.read(), prefix=" " * 16)
        else:
            base_commands = "FROM {base_image}".format(base_image=self.base_image)

        file_contents = textwrap.dedent(
            """\
            {base_commands}

            RUN pip install pip --upgrade
            {extra_commands}
            {pip_installs}

            RUN mkdir -p /root/.prefect/
            {copy_flows}
            COPY {healthcheck_loc} /root/.prefect/healthcheck.py
            {copy_files}

            {env_vars}

            RUN python /root/.prefect/healthcheck.py '[{flow_file_paths}]' '{python_version}'
            """.format(
                base_commands=base_commands,
                extra_commands=extra_commands,
                pip_installs=pip_installs,
                copy_flows=copy_flows,
                healthcheck_loc=healthcheck_loc
                if self.dockerfile
                else "healthcheck.py",
                copy_files=copy_files,
                env_vars=env_vars,
                flow_file_paths=", ".join(
                    ['"{}"'.format(k) for k in self.flows.values()]
                ),
                python_version=(sys.version_info.major, sys.version_info.minor),
            )
        )

        file_contents = "\n".join(line.lstrip() for line in file_contents.split("\n"))
        dockerfile_path = os.path.join(directory, "Dockerfile")
        with open(dockerfile_path, "w+") as dockerfile:
            dockerfile.write(file_contents)
        return dockerfile_path

    ########################
    # Docker Utilities
    ########################

    def pull_image(self) -> None:
        """Pull the image specified so it can be built.

        In order for the docker python library to use a base image it must be pulled
        from either the main docker registry or a separate registry that must be set as
        `registry_url` on this class.

        Raises:
            - InterruptedError: if either pulling the image fails
        """
        client = docker.APIClient(base_url=self.base_url, version="auto")

        output = client.pull(self.base_image, stream=True, decode=True)
        for line in output:
            if line.get("error"):
                raise InterruptedError(line.get("error"))
            if line.get("progress"):
                print(line.get("status"), line.get("progress"), end="\r")
        print("")

    def push_image(self, image_name: str, image_tag: str) -> None:
        """Push this environment to a registry

        Args:
            - image_name (str): Name for the image
            - image_tag (str): Tag for the image

        Raises:
            - InterruptedError: if either pushing the image fails
        """
        client = docker.APIClient(base_url=self.base_url, version="auto")

        logging.info("Pushing image to the registry...")

        output = client.push(image_name, tag=image_tag, stream=True, decode=True)
        for line in output:
            if line.get("error"):
                raise InterruptedError(line.get("error"))
            if line.get("progress"):
                print(line.get("status"), line.get("progress"), end="\r")
        print("")

    def _parse_generator_output(self, generator: Iterable) -> None:
        """
        Parses and writes a Docker command's output to stdout
        """
        for item in generator:
            item = item.decode("utf-8")
            for line in item.split("\n"):
                if line:
                    output = json.loads(line).get("stream") or json.loads(line).get(
                        "errorDetail", {}
                    ).get("message")
                    if output and output != "\n":
                        print(output.strip("\n"))
