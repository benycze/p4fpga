# Copyright (c) Barefoot Networks, Inc.
# Licensed under the Apache License, Version 2.0 (the "License")

from p4_hlir.hlir import p4_header_instance, p4_table, \
     p4_conditional_node, p4_action, p4_parse_state
from p4_hlir.main import HLIR
import llvmInstance
import llvmParser
import llvmDeparser
import typeFactory
import programSerializer
from compilationException import *


class LLVMProgram(object):
    def __init__(self, name, hlir):
        assert isinstance(hlir, HLIR)

        self.hlir = hlir
        self.name = name
        self.uniqueNameCounter = 0
        self.reservedPrefix = "llvm_"

        self.packetName = self.reservedPrefix + "packet"
        self.dropBit = self.reservedPrefix + "drop"
        self.license = "MIT"
        self.offsetVariableName = self.reservedPrefix + "packetOffsetInBits"
        self.zeroKeyName = self.reservedPrefix + "zero"
        # all array tables must be indexed with u32 values

        self.errorName = self.reservedPrefix + "error"
        self.functionName = self.reservedPrefix + "filter"
        self.egressPortName = "egress_port" # Hardwired in P4 definition

        self.typeFactory = typeFactory.LLVMTypeFactory()
        self.errorCodes = [
            "p4_pe_no_error",
            "p4_pe_index_out_of_bounds",
            "p4_pe_out_of_packet",
            "p4_pe_header_too_long",
            "p4_pe_header_too_short",
            "p4_pe_unhandled_select",
            "p4_pe_checksum"]

        self.actions = []
        self.conditionals = []
        self.tables = []
        self.headers = []   # header instances
        self.metadata = []  # metadata instances
        self.stacks = []    # header stack instances LLVMHeaderStack
        self.parsers = []   # all parsers
        self.deparser = None
        self.entryPoints = []  # control-flow entry points from parser
        self.counters = []
        self.entryPointLabels = {}  # maps p4_node from entryPoints
                                    # to labels in the C program
        self.egressEntry = None

        self.construct()

        self.headersStructTypeName = self.reservedPrefix + "headers_t"
        self.headerStructName = self.reservedPrefix + "headers"
        self.metadataStructTypeName = self.reservedPrefix + "metadata_t"
        self.metadataStructName = self.reservedPrefix + "metadata"

    def construct(self):
        if len(self.hlir.p4_field_list_calculations) > 0:
            raise NotSupportedException(
                "{0} calculated field",
                self.hlir.p4_field_list_calculations.values()[0].name)

        for h in self.hlir.p4_header_instances.values():
            if h.max_index is not None:
                assert isinstance(h, p4_header_instance)
                if h.index == 0:
                    # header stack; allocate only for zero-th index
                    indexVarName = self.generateNewName(h.base_name + "_index")
                    stack = llvmInstance.LLVMHeaderStack(
                        h, indexVarName, self.typeFactory)
                    self.stacks.append(stack)
            elif h.metadata:
                metadata = llvmInstance.LLVMMetadata(h, self.typeFactory)
                self.metadata.append(metadata)
            else:
                header = llvmInstance.LLVMHeader(h, self.typeFactory)
                self.headers.append(header)

        for p in self.hlir.p4_parse_states.values():
            parser = llvmParser.LLVMParser(p)
            self.parsers.append(parser)

        for n in self.hlir.p4_ingress_ptr.keys():
            self.entryPoints.append(n)

        for n in self.hlir.p4_conditional_nodes.values():
            conditional = llvmConditional.LLVMConditional(n, self)
            self.conditionals.append(conditional)

        self.egressEntry = self.hlir.p4_egress_ptr
        self.deparser = llvmDeparser.LLVMDeparser(self.hlir)

    def isInternalAction(self, action):
        # This is a heuristic really to guess which actions are built-in
        # Unfortunately there seems to be no other way to do this
        return action.lineno < 0

    @staticmethod
    def isArrayElementInstance(headerInstance):
        assert isinstance(headerInstance, p4_header_instance)
        return headerInstance.max_index is not None

    def emitWarning(self, formatString, *message):
        assert isinstance(formatString, str)
        print("WARNING: ", formatString.format(*message))

    # noinspection PyMethodMayBeStatic
    def generateIncludes(self, serializer):
        assert isinstance(serializer, programSerializer.ProgramSerializer)
        serializer.append(self.config.getIncludes())

    def getLabel(self, p4node):
        # C label that corresponds to this point in the control-flow
        if p4node is None:
            return "end"
        elif isinstance(p4node, p4_parse_state):
            label = p4node.name
            self.entryPointLabels[p4node.name] = label
        if p4node.name not in self.entryPointLabels:
            label = self.generateNewName(p4node.name)
            self.entryPointLabels[p4node.name] = label
        return self.entryPointLabels[p4node.name]

    def generateNewName(self, base):  # base is a string
        """Generates a fresh name based on the specified base name"""
        # TODO: this should be made "safer"
        assert isinstance(base, str)

        base += "_" + str(self.uniqueNameCounter)
        self.uniqueNameCounter += 1
        return base

    def generateTables(self, serializer):
        assert isinstance(serializer, programSerializer.ProgramSerializer)

        for t in self.tables:
            t.serialize(serializer, self)

        for c in self.counters:
            c.serialize(serializer, self)

    def generateHeaderInstance(self, serializer):
        assert isinstance(serializer, programSerializer.ProgramSerializer)

        serializer.emitIndent()
        serializer.appendFormat(
            "struct {0} {1}", self.headersStructTypeName, self.headerStructName)

    def generateInitializeHeaders(self, serializer):
        assert isinstance(serializer, programSerializer.ProgramSerializer)

        serializer.blockStart()
        for h in self.headers:
            serializer.emitIndent()
            serializer.appendFormat(".{0} = ", h.name)
            h.type.emitInitializer(serializer)
            serializer.appendLine(",")
        serializer.blockEnd(False)

    def generateMetadataInstance(self, serializer):
        assert isinstance(serializer, programSerializer.ProgramSerializer)

        serializer.emitIndent()
        serializer.appendFormat(
            "struct {0} {1}",
            self.metadataStructTypeName,
            self.metadataStructName)

    def generateDeparser(self, serializer):
        self.deparser.serialize(serializer, self)

    def generateInitializeMetadata(self, serializer):
        assert isinstance(serializer, programSerializer.ProgramSerializer)

        serializer.blockStart()
        for h in self.metadata:
            serializer.emitIndent()
            serializer.appendFormat(".{0} = ", h.name)
            h.emitInitializer(serializer)
            serializer.appendLine(",")
        serializer.blockEnd(False)

    def getStackInstance(self, name):
        assert isinstance(name, str)

        for h in self.stacks:
            if h.name == name:
                assert isinstance(h, llvmInstance.LLVMHeaderStack)
                return h
        raise CompilationException(
            True, "Could not locate header stack named {0}", name)

    def getHeaderInstance(self, name):
        assert isinstance(name, str)

        for h in self.headers:
            if h.name == name:
                assert isinstance(h, llvmInstance.LLVMHeader)
                return h
        raise CompilationException(
            True, "Could not locate header instance named {0}", name)

    def getInstance(self, name):
        assert isinstance(name, str)

        for h in self.headers:
            if h.name == name:
                return h
        for h in self.metadata:
            if h.name == name:
                return h
        raise CompilationException(
            True, "Could not locate instance named {0}", name)

    def getTable(self, name):
        assert isinstance(name, str)
        for t in self.tables:
            if t.name == name:
                return t
        raise CompilationException(
            True, "Could not locate table named {0}", name)

    def getConditional(self, name):
        assert isinstance(name, str)
        for c in self.conditionals:
            if c.name == name:
                return c
        raise CompilationException(
            True, "Could not locate conditional named {0}", name)

    def generateParser(self, serializer):
        assert isinstance(serializer, programSerializer.ProgramSerializer)
        for p in self.parsers:
            p.serialize(serializer, self)

    def generateIngressPipeline(self, serializer):
        assert isinstance(serializer, programSerializer.ProgramSerializer)
        # Generate Tables

    def generateControlFlowNode(self, serializer, node, nextEntryPoint):
        pass
        # generate control flow

    def generatePipelineInternal(self, serializer, nodestoadd, nextEntryPoint):
        assert isinstance(serializer, programSerializer.ProgramSerializer)
        assert isinstance(nodestoadd, set)

        done = set()
        while len(nodestoadd) > 0:
            todo = nodestoadd.pop()
            if todo in done:
                continue
            if todo is None:
                continue

            print("Generating ", todo.name)

            done.add(todo)
            self.generateControlFlowNode(serializer, todo, nextEntryPoint)

            for n in todo.next_.values():
                nodestoadd.add(n)

    def generatePipeline(self, serializer):
        todo = set()
        for e in self.entryPoints:
            todo.add(e)
        self.generatePipelineInternal(serializer, todo, self.egressEntry)
        todo = set()
        todo.add(self.egressEntry)
        self.generatePipelineInternal(serializer, todo, None)