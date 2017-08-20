"""Provides a server connection to a CMake executable."""
import subprocess
import json
import uuid
from conans.errors import ConanException
from conans import CMake

class CMakeServer(object):
    """Represents a CMake server connection and facilitates communication with that server to
       provide configure, build, and install steps as well as to enable extraction of information
       from the CMake server."""

    def __init__(self, conanfile, generator, platform, toolset,
                 source_dir=None, build_dir=None, parallel=False):
        """Creates a new CMakeServer instance to configure, build, and extract information
           from the build process specified by the given conanfile
           :param conanfile the conanfile which describes the recipe to be built
           :param generator the generator to use
           :param platform the platform we are building for
           :param toolset the toolset we are using, or None if not relevant
           :param source_dir the source directory to use. Optional. If not specified, will be
                             set to conanfile.source_folder
           :param build_dir the build directory to use. Optional. If not specified, will be set
                            to conanfile.build_folder
           :param parallel True if this build process should run parallelized, or False otherwise.
                           Optional. Defaults to False.
        """
        self._conanfile = conanfile
        self._debug = True

        self._os = conanfile.settings.get_safe("os")
        self._compiler = conanfile.settings.get_safe("compiler")
        self._compiler_version = conanfile.settings.get_safe("compiler.version")
        self._arch = conanfile.settings.get_safe("arch")
        self._build_type = conanfile.settings.get_safe("build_type")
        self._op_system_version = conanfile.settings.get_safe("os.version")
        self._libcxx = conanfile.settings.get_safe("compiler.libcxx")
        self._runtime = conanfile.settings.get_safe("compiler.runtime")

        self._build_folder = build_dir or conanfile.build_folder
        self._source_folder = source_dir or conanfile.source_folder
        self._generator = generator
        self._platform = platform
        self._toolset = toolset
        self._parallel = parallel

        self._helper = CMake(conanfile,
                             generator=generator,
                             parallel=parallel)
        self._server = None


    # Using __enter__ here so that we are sure we clean up the process if we break out
    # of the command unexpectedly
    def __enter__(self):
        # Start the server
        self._server = subprocess.Popen(["cmake", "-E", "server", "--experimental", "--debug"],
                                        stdin=PIPE, #pylint:disable=E0602
                                        stdout=PIPE, #pylint:disable=E0602
                                        stderr=PIPE, #pylint:disable=E0602
                                        bufsize=1, # line buffered
                                        universal_newlines=True)

        # check version information / that we successfully got a server
        # (the server should give us a 'hello' immediately)
        hello = self._get_next_object()
        if hello["type"] != "hello":
            # Something went wrong
            raise ConanException("CMake server communication failed: expected 'hello', got '%s'" %
                                 str(hello["type"]))

        # Select the highest 1.x version available
        major = 0
        minor = 0
        for version in hello["supportedProtocolVersions"]:
            if version["major"] == 1:
                major = version["major"]
                if version["minor"] > minor:
                    minor = version["minor"]

        if major == 0:
            # No 1.x version was available!
            raise ConanException("CMake server communication failed: no 1.x protocol available")

        # Send the handshake reply with the requested protocol version
        self._issue_command_and_await_reply({
            "type": "handshake",
            "protocolVersion": {"major": major, "minor": minor},
            "sourceDirectory": self._source_folder,
            "buildDirectory": self._build_folder,
            "generator": self._generator,
            "platform": self._platform,
            "toolset": self._toolset
        })

        return self

    # Cleans up the command server
    def __exit__(self, exc_type, exc_value, traceback):
        # Check if the server is still running
        self._server.poll()
        if self._server.returncode != None:
            # Terminate the server
            self._server.terminate()

        self._server = None

    # Gets the next CMake server message object from the server and returns it as a dictionary
    def _get_next_object(self):
        # verify the server is still alive
        self._check_server_liveliness()

        # scan for the header
        for line in self._server.stdout:
            if line == "[== \"CMake Server\" ==[":
                # Found the header
                break
            else:
                # Keep looking, but also verify the server is still alive
                self._check_server_liveliness()

        # Read in the JSON lines until we reach the footer
        object_string = ""
        for line in self._server.stdout:
            if line == "]== \"CMake Server\" ==]":
                # Found the footer - end of object
                break
            else:
                object_string += line
                self._check_server_liveliness()

        # Parse the JSON string into a dictionary
        return json.loads(object_string)

    # Sends the specified command to the server
    def _issue_command(self, command):
        """Sends the specified command to the server
           :param command a dictionary object representing the command to send"""
        # verify the server is still alive
        self._check_server_liveliness()

        # format the command
        json_command = json.dumps(command)

        # send the command
        self._server.stdin.writelines([
            "[== \"CMake Server\" ==[",
            json_command,
            "]== \"CMake Server\" ==]"
        ])



    def _issue_command_and_await_reply(self, command):
        """Sends the specified command to the server and awaits for the reply.
           The reply is then returned.
           :param command a dictionary object representing the command to send"""

        # Generate a random cookie to help with identifying the reply
        cookie = str(uuid.uuid4())
        command["cookie"] = cookie

        # Issue the command
        self._issue_command(command)

        # Await a reply
        result = None
        while result is None:
            result = self._get_next_object()
            if result["cookie"] != cookie:
                result = None           # Await for the reply that includes our cookie

        return result


    def _check_server_liveliness(self):
        """Verifies that the server is still running. If it is not running, an exception
           is thrown."""
        self._server.poll()
        if self._server.returncode != None:
            raise ConanException("CMake server exited unexpectedly")
