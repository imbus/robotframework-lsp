import sys
from typing import TypeVar, Any, Optional, List, Sequence
from robocorp_ls_core.protocols import (
    Sentinel,
    IMonitor,
    IDocument,
    IWorkspace,
    IConfig,
    IDocumentSelection,
)
from robocorp_ls_core.constants import NULL
from collections import namedtuple
from robotframework_ls.impl.robot_specbuilder import KeywordArg

if sys.version_info < (3, 8):

    class Protocol(object):
        pass


else:
    from typing import Protocol

T = TypeVar("T")


NodeInfo = namedtuple("NodeInfo", "stack, node")
TokenInfo = namedtuple("TokenInfo", "stack, node, token")
KeywordUsageInfo = namedtuple("KeywordUsageInfo", "stack, node, token, name")


class IRobotDocument(IDocument, Protocol):
    def get_type(self) -> str:
        ...

    def get_ast(self) -> Any:
        ...

    symbols_cache: Optional[list] = None


class ILibspecManager(Protocol):
    def get_library_info(self, libname, create=True, current_doc_uri=None, arguments=None, alias=None):
        ...

    def get_library_error(self, libname,  current_doc_uri=None, arguments=None, alias=None):
        ...

    def get_library_warning(self, libname,  current_doc_uri=None, arguments=None, alias=None):
        ...

    def add_workspace_folder(self, folder_uri: str):
        ...

    def remove_workspace_folder(self, folder_uri: str):
        ...

    @property
    def root_uri(self) -> str:
        ...

    @root_uri.setter
    def root_uri(self, value: str):
        ...


class IRobotWorkspace(IWorkspace, Protocol):

    @property
    def libspec_manager(self) -> ILibspecManager:
        ...

    @libspec_manager.setter
    def libspec_manager(self, value: ILibspecManager):
        ...


class IKeywordFound(Protocol):
    """
    :ivar completion_context:
        This may be a new completion context, created when a new document is
        being analyzed (the keyword was created for that completion context).
        For libraries the initial completion context is passed.
    :ivar source:
        Source where the keyword was found.
    :ivar lineno:
        Line where it was found (0-based). 
    """

    @property
    def keyword_name(self) -> str:
        pass

    @property
    def keyword_args(self) -> Sequence[KeywordArg]:
        pass

    @property
    def docs(self) -> str:
        pass

    @property
    def docs_format(self) -> str:
        pass

    completion_context: Optional["ICompletionContext"]

    completion_item_kind: int = -1

    @property
    def source(self) -> str:
        pass

    @property
    def lineno(self) -> int:
        pass

    @property
    def end_lineno(self) -> int:
        pass

    @property
    def col_offset(self) -> int:
        pass

    @property
    def end_col_offset(self) -> int:
        pass

    @property
    def library_name(self) -> Optional[str]:
        # If it's a library, this is the name of the library.
        pass

    @property
    def resource_name(self) -> Optional[str]:
        # If it's a resource, this is the basename of the resource without the extension.
        pass


class IKeywordCollector(Protocol):
    def accepts(self, keyword_name: str) -> bool:
        """
        :param keyword_name:
            The name of the keyword to be accepted or not.
        :return bool:
            If the return is True, on_keyword(...) is called (otherwise it's not
            called).
        """

    def on_keyword(self, keyword_found: IKeywordFound):
        """
        :param IKeywordFound keyword_found:
        """


class IDefinition(Protocol):

    keyword_name: str = ""  # Can be empty if it's not found as a keyword.

    # Note: Could be None (i.e.: we found it in a library spec file which doesn't have the source).
    source: str = ""

    # Note: Could be None (i.e.: we found it in a library spec file which doesn't have the lineno).
    lineno: Optional[int] = -1

    # Note: Could be None (i.e.: we found it in a library spec file which doesn't have the lineno).
    end_lineno: Optional[int] = -1

    col_offset: Optional[int] = -1

    end_col_offset: Optional[int] = -1


class IKeywordDefinition(IDefinition, Protocol):
    keyword_found: IKeywordFound


class ICompletionContext(Protocol):
    def __init__(
        self,
        doc,
        line=Sentinel.SENTINEL,
        col=Sentinel.SENTINEL,
        workspace=None,
        config=None,
        memo=None,
        monitor: IMonitor = NULL,
    ) -> None:
        """
        :param robocorp_ls_core.workspace.Document doc:
        :param int line:
        :param int col:
        :param RobotWorkspace workspace:
        :param robocorp_ls_core.config.Config config:
        :param _Memo memo:
        """

    @property
    def monitor(self) -> IMonitor:
        ...

    def check_cancelled(self):
        ...

    def create_copy_with_selection(self, line: int, col: int) -> "ICompletionContext":
        ...

    def create_copy(self, doc: IRobotDocument) -> "ICompletionContext":
        ...

    @property
    def original_doc(self) -> IRobotDocument:
        ...

    @property
    def original_sel(self) -> Any:
        ...

    @property
    def doc(self) -> IRobotDocument:
        ...

    @property
    def sel(self) -> IDocumentSelection:
        ...

    @property
    def memo(self) -> Any:
        pass

    @property
    def config(self) -> Optional[IConfig]:
        pass

    @property
    def workspace(self) -> IRobotWorkspace:
        ...

    def get_type(self) -> Any:
        ...

    def get_ast(self) -> Any:
        ...

    def get_ast_current_section(self) -> Any:
        """
        :rtype: robot.parsing.model.blocks.Section|NoneType
        """

    def get_accepted_section_header_words(self) -> List[str]:
        ...

    def get_current_section_name(self) -> Optional[str]:
        ...

    def get_current_token(self) -> Optional[TokenInfo]:
        ...

    def get_current_variable(self, section=None) -> Optional[TokenInfo]:
        ...

    def get_resource_import_as_doc(self, resource_import) -> Optional[IRobotDocument]:
        ...

    def get_current_keyword_definition(self) -> Optional[IKeywordDefinition]:
        ...
