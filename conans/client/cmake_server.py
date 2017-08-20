"""Provides a server connection to a CMake executable."""
import subprocess
import json
import uuid
from conans.errors import ConanException
from conans import CMake

class CMakeServer(CMake):
    """Represents a CMake server connection and facilitates communication with that server to
       provide configure, build, and install steps as well as to enable extraction of information
       from the CMake server."""

    def __init__(self, conanfile, generator=None, platform=None, toolset=None,
                 source_dir=None, build_dir=None, parallel=False):
        """Creates a new CMakeServer instance to configure, build, and extract information
           from the build process specified by the given conanfile
           :param conanfile the conanfile which describes the recipe to be built
           :param generator the generator to use. Optional. If not specified, will be auto-detected.
           :param platform the platform we are building for. Optional. If not specified, will be auto-detected.
           :param toolset the toolset we are using, or None if not relevant. Optional. If not specified,
                          will use the default toolset if the generator needs a toolset selected.
           :param source_dir the source directory to use. Optional. If not specified, will be
                             set to conanfile.source_folder
           :param build_dir the build directory to use. Optional. If not specified, will be set
                            to conanfile.build_folder
           :param parallel True if this build process should run parallelized, or False otherwise.
                           Optional. Defaults to False.
        """
        super(CMakeServer, self).__init__(conanfile,
                                          generator=generator,
                                          parallel=parallel,
                                          cmake_system_name=platform or True)
        self._debug = False
        self._build_folder = self._convert_to_forward_slashes(build_dir or conanfile.build_folder)
        self._source_folder = self._convert_to_forward_slashes(source_dir or conanfile.source_folder)
        self._server = None
        self._toolset = toolset
        self._conanfile = conanfile
        self.configure = self._configure # required because of CMake implementation of configure()


    # Using __enter__ here so that we are sure we clean up the process if we break out
    # of the command unexpectedly
    def __enter__(self):
        # Start the server
        self._server = subprocess.Popen(["cmake", "-E", "server", "--experimental", "--debug"],
                                        stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE,
                                        bufsize=0, # line buffered
                                        universal_newlines=True)

        self._conanfile.output.info("Establishing CMake server connection...")

        # check version information / that we successfully got a server
        # (the server should give us a 'hello' immediately)
        hello = self._get_next_object()
        if hello["type"] != "hello":
            # Something went wrong
            raise ConanException("CMake server communication failed: expected 'hello', got '%s'" %
                                 str(hello["type"]))

        # Select the highest 1.x version available
        self._debug_output("Supported protocols: %s" % str(hello["supportedProtocolVersions"]))
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
        handshake = {
            "type": "handshake",
            "protocolVersion": {"major": major, "minor": minor},
            "sourceDirectory": self._source_folder,
            "buildDirectory": self._build_folder,
            "generator": self._generator(),
            "toolset": self._toolset
        }
        if "Visual Studio" not in handshake["generator"]:   # VS generator has platform configured inherently
            handshake["platform"] = self._os

        self._issue_command_and_await_reply(handshake, required=True)
        self._conanfile.output.info("CMake Server connection established (protocol %s.%s)" % (str(major), str(minor)))

        # Set appropriate settings
        self._issue_command_and_await_reply({
            "type": "setGlobalSettings",
            "sourceDirectory": self._source_folder,
            "buildDirectory": self._build_folder,
            "currentGenerator": self._generator()
        }, required=True)

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
        stdout = iter(self._server.stdout.readline, '') # http://bugs.python.org/issue3907
        for line in stdout:
            self._debug_output(line.strip())
            if line == "[== \"CMake Server\" ==[\n":
                # Found the header
                break
            else:
                # Keep looking, but also verify the server is still alive
                self._check_server_liveliness()
        # Read in the JSON lines until we reach the footer
        object_string = ""
        for line in stdout:
            self._debug_output(line.strip())
            if line == "]== \"CMake Server\" ==]\n":
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
        self._debug_output("CMD> %s" % json_command)
        self._server.stdin.writelines([
            "[== \"CMake Server\" ==[\n",
            json_command, "\n",
            "]== \"CMake Server\" ==]\n"
        ])
        self._server.stdin.write("\n")
        self._server.stdin.flush()



    def _issue_command_and_await_reply(self, command, required=False):
        """Sends the specified command to the server and awaits for the reply.
           The reply is then returned.
           :param command a dictionary object representing the command to send
           :param required Optional. Defaults to False. If True, will perform
                  a check on the result and raise an exception and output
                  a message if the command failed. Otherwise, no check
                  is performed."""

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
            elif result["type"] == "message":
                # Output message to user
                self._conanfile.output.writeln(result["message"])
                result = None           # Await for a completion reply
            elif result["type"] == "progress":
                result = None           # we don't do anything with progress messages right now
            elif result["type"] != "error" and result["type"] != "reply":
                raise ConanException("Unknown CMake message type '%s'" % result["type"])

        if required:
            self._raise_if_error(result)

        return result


    def _check_server_liveliness(self):
        """Verifies that the server is still running. If it is not running, an exception
           is thrown."""
        if self._server is None:
            raise ConanException("attempt to access cmake server outside of 'with' block")

        self._server.poll()
        if self._server.returncode != None:
            raise ConanException("CMake server exited unexpectedly")


    def _configure(self, defs=None):
        """Runs the 'configure' step of CMake.
           :param defs a dictionary of definitions to pass to CMake. Optional. Defaults to no additional
                       definitions."""

        # Build the list of definitions
        cache_arguments = []
        if defs is not None:
            for key, value in defs.items():
                cache_arguments.append("-D%s=%s" % (key, value))

        # Issue configure
        self._issue_command_and_await_reply({
            "type": "configure",
            "cacheArguments": cache_arguments
        }, required=True)

        # Generate
        self._issue_command_and_await_reply({"type": "compute"}, required=True)





    def _raise_if_error(self, result):
        """Checks the specified CMake result object to see if it is an error. If it is,
           an error is posted to output and an exception is raised.
           :param result the result object returned by a call to self._issue_command_and_await_reply"""
        if result["type"] == "error":
            self._conanfile.output.error("CMake Server: %s" % result["errorMessage"])
            raise ConanException("CMake Server error")


    def _debug_output(self, message):
        """If self._debug is True, outputs the specified debug message, otherwise does nothing.
           :param message the message to output"""
        if self._debug:
            self._conanfile.output.info(message)

    def _convert_to_forward_slashes(self, path):
        """Converts a path to a path with forward slashes (for CMake compatibility)
           :param path the string to replace characters in"""
        return path.replace('\\', '/')
