#!/usr/bin/env python3
import os
import sys
import subprocess
import logging
import shutil
from collections import defaultdict
import multiprocessing

from fasta_utils import read_fasta_file_handle, format_seq
from fastq_utils import read_fastq_file_handle



logger = logging.getLogger(__name__)

def extract_reads_by_component(fastq, read_metanode_component_filepath):
    # Reading read --> component file
    logger.debug('Reading read-->component from {}'.format(read_metanode_component_filepath))
    read_component_dict = dict()
    with open(read_metanode_component_filepath, 'r') as read_metanode_component_fh:
        read_component_dict = {t[0]:t[2] for t in (l.split() for l in read_metanode_component_fh) if t[2] != 'NULL'}

    # Storing reads for each component
    logger.debug('Storing reads by component from {}'.format(fastq))
    component_reads_dict = defaultdict(list)
    with open(fastq, 'r') as fastq_fh:
        for header, seq, qual in read_fastq_file_handle(fastq_fh):
            try:
                component_reads_dict[read_component_dict[header]].append(tuple((header, seq, qual)))
            except KeyError:
                pass
    return component_reads_dict


def extract_lca_by_component(components_lca_filepath):
    # Reading components LCA and storing them in a dict
    logger.debug('Reading components LCA assignment from {0}'.format(components_lca_filepath))
    component_lca_dict = dict()
    with open(components_lca_filepath, 'r') as component_lca_fh:
        component_lca_dict = {t[0]:t[1] for t in (l.split() for l in component_lca_fh) if len(t) == 2}
    return component_lca_dict

def save_components(component_reads_dict, directory):
    """
    Take a dict (key=component_id, value=[header,seq,qual])
    and save each component to a file into the given directory:
    directory/
        component_%s.fq % component_id

    Return a dict (key=component_id, value=fastq_path)
    """

    try:
        os.mkdir(directory)
    except FileExistsError as fee:
        pass

    components_fq = {}
    for component_id, reads_list in component_reads_dict.items():
        fq_name = "component%s_reads.fq" % component_id
        fq_path = os.path.join(directory, fq_name)
        #logger.debug("Save component %s into %s" % (component_id, fq_path))
        components_fq[component_id] = fq_path

        with open(fq_path, 'w') as component_fh:
            for (header, seq, qual) in reads_list:
                component_fh.write('@{}\n{}\n+\n{}\n'.format(header, seq, qual))
    return components_fq


def assemble_component(sga_wrapper, sga_bin,
                       in_fastq, workdir,
                       read_correction, cpu):
    #logger.debug('Assembling: %s' % in_fastq)
    if not os.path.isfile(in_fastq):
        logger.fatal('The input reads file does not exists:%s' % in_fastq)
        sys.exit("Can't assemble %s" % in_fastq)

    if os.path.isdir(workdir):
        #logger.debug("Remove previous SGA working dir before assembling:%s" % workdir)
        shutil.rmtree(workdir)
    os.mkdir(workdir)

    logfile = os.path.join(workdir, 'assembly.log')
    tmp_dir = os.path.join(workdir, 'tmp')
    fasta_file = os.path.join(workdir, 'assembly.fasta')

    cmd_line = 'echo "component #' + in_fastq + '" >> '
    cmd_line += logfile + ' && '
    cmd_line += sga_wrapper + ' -i ' + in_fastq
    cmd_line += ' -o ' + fasta_file + ' --sga_bin ' + sga_bin
    if read_correction in ('no', 'auto'):
        cmd_line += ' --no_correction' # !!! desactivate all SGA error corrections and filters
    cmd_line += ' --cpu ' + str(cpu)
    cmd_line += ' --tmp_dir %s' % tmp_dir
    cmd_line += ' >> ' + logfile + ' 2>&1'

    #logger.debug('CMD: {0}'.format(cmd_line))
    rc = subprocess.call(cmd_line, shell=True, bufsize=0)
    if rc != 0:
        logger.fatal('Failed to assemble the component: %s' % (in_fastq))
        logger.fatal('See %s for more info' % logfile)
        sys.exit('Assembly failed')

    return fasta_file


def concat_components_fasta_with_lca(assembled_components_fasta, contigs_fasta, component_lca_dict):
    if os.path.isfile(contigs_fasta):
        #logger.debug("Remove old contig fasta file:%s" % contigs_fasta)
        os.unlink(contigs_fasta)

    component_lca = 'NULL'
    contigs_fh = open(contigs_fasta, 'w')
    contig_count = 0
    for component_id, component_fasta in assembled_components_fasta.items():
        if component_id in component_lca_dict:
            component_lca = component_lca_dict[component_id]
        with open(component_fasta, 'r') as sga_contigs_fh:
            for header, seq in read_fasta_file_handle(sga_contigs_fh):
                if len(seq):
                    contig_count += 1
                    contigs_fh.write('>{0} component={1} '.format(contig_count, component_id))
                    contigs_fh.write('lca={0}\n{1}\n'.format(component_lca, format_seq(seq)))

    contigs_fh.close()


def _get_workdir(fq):
    """
    Convenient function to build a workdir from the name of the fastqfile
    """
    sga_wkdir_basename, _ = os.path.splitext(fq)
    return '%s_assembly_wkdir' % sga_wkdir_basename


def assemble_all_components(sga_wrapper, sga_bin,
                            fastq, read_metanode_component_filepath, components_lca_filepath,
                            out_contigs_fasta, workdir,
                            cpu, read_correction):


    logger.info("Save components to fastq files")
    components_dict = extract_reads_by_component(fastq, read_metanode_component_filepath)
    components_reads_fq = save_components(components_dict, workdir).items()
    assembled_components_fasta = {}

    logger.info("Assemble components")

    # Foreach component, build the parameters used by assemble_component and save
    # them into a list to be able to apply a map function on it
    params = []
    component_id_list = []
    for component_id, fq in components_reads_fq:
        params.append((sga_wrapper, sga_bin, fq, _get_workdir(fq), read_correction, 1))
        component_id_list.append(component_id)

    with multiprocessing.Pool(processes=cpu) as pool:
        fasta_list = pool.starmap(assemble_component, params)

    # Make the correspondance between the component_id and the fasta file
    assembled_components_fasta = dict(zip(component_id_list, fasta_list))

    logger.info("Pool components contigs into: %s" % out_contigs_fasta)
    lca_dict = extract_lca_by_component(components_lca_filepath)
    concat_components_fasta_with_lca(assembled_components_fasta,
                                     out_contigs_fasta, lca_dict)