#!/usr/bin/env python3

import argparse
import os
import sys
import bioannotation
import biocodeutils
import biothings
import sqlite3
import re

## constants
DEFAULT_PRODUCT_NAME = "hypothetical protein"

def main():
    parser = argparse.ArgumentParser( description='Parses multiple sources of evidence to generate a consensus functional annotation')

    ## output file to be written
    parser.add_argument('-f', '--input_fasta', type=str, required=True, help='Protein FASTA file of source molecules' )
    parser.add_argument('-m', '--hmm_htab_list', type=str, required=True, help='List of htab files from hmmpfam3' )
    parser.add_argument('-b', '--blast_btab_list', type=str, required=True, help='List of btab files from BLAST' )
    parser.add_argument('-o', '--output_tab', type=str, required=False, help='Optional output tab file path (else STDOUT)' )
    parser.add_argument('-d', '--hmm_db', type=str, required=True, help='SQLite3 db with HMM information' )
    parser.add_argument('-u', '--unitprot_sprot_db', type=str, required=True, help='SQLite3 db with UNITPROT/SWISSPROT information' )
    args = parser.parse_args()

    hmm_db_conn = sqlite3.connect(args.hmm_db)
    hmm_db_curs = hmm_db_conn.cursor()

    usp_db_conn = sqlite3.connect(args.unitprot_sprot_db)
    usp_db_curs = usp_db_conn.cursor()

    polypeptides = initialize_polypeptides( args.input_fasta )

    print("INFO: parsing HMM evidence")
    parse_hmm_evidence( polypeptides, args.hmm_htab_list, hmm_db_curs )

    print("INFO: parsing BLAST evidence")
    parse_blast_evidence( polypeptides, args.blast_btab_list, usp_db_curs )

    hmm_db_curs.close()

    ## output will either be a file or STDOUT
    fout = sys.stdout
    if args.output_tab is not None:
        fout = open(args.output_tab, 'wt')

    fout.write("polypeptide ID\tpolypeptide_length\tgene_product_name\tGO_terms\tEC_nums\n")

    for polypeptide_id in polypeptides:
        polypeptide = polypeptides[polypeptide_id]
        go_string = ""
        ec_string = ""

        for go_annot in polypeptide.annotation.go_annotations:
            go_string += "GO:{0},".format(go_annot.go_id)
        go_string = go_string.rstrip(',')

        for ec_annot in polypeptide.annotation.ec_numbers:
            ec_string += "{0},".format(ec_annot.number)
        ec_string = ec_string.rstrip(',')
                
        fout.write( "{0}\t{1}\t{2}\t{3}\t{4}\n".format( \
                polypeptide_id, polypeptide.length, polypeptide.annotation.product_name, go_string, ec_string) )

    fout.close()

def initialize_polypeptides( fasta_file ):
    '''
    Reads a FASTA file of (presumably) polypeptide sequences and creates a dict of Polypeptide
    objects, keyed by ID.  No bioannotation.FunctionalAnnotation objects will be attached yet.
    '''
    seqs = biocodeutils.fasta_dict_from_file( fasta_file )

    polypeptides = dict()

    for seq_id in seqs:
        polypeptide = biothings.Polypeptide( id=seq_id, length=len(seqs[seq_id]['s']) )
        annotation = bioannotation.FunctionalAnnotation(product_name=DEFAULT_PRODUCT_NAME)
        polypeptide.annotation = annotation
        
        polypeptides[seq_id] = polypeptide
    
    return polypeptides


def parse_blast_evidence( polypeptides, blast_list, cursor ):
    '''
    Reads a list file of NCBI BLAST evidence and a dict of polypeptides, populating
    each with Annotation evidence where appropriate.  Only attaches evidence if
    the product name is the default.

    Currently only considers the top BLAST hit for each query.
    '''
    for file in biocodeutils.read_list_file(blast_list):
        last_qry_id = None
        
        for line in open(file):
            line = line.rstrip()
            cols = line.split("\t")
            this_qry_id = cols[0]

            # the BLAST hits are sorted already with the top hit for each query first
            if last_qry_id != this_qry_id:
                annot = polypeptides[this_qry_id].annotation

                # get the accession from the cols[5]
                #  then process for known accession types
                accession = cols[5]

                if accession.startswith('sp|'):
                    # pluck the second part out of this:
                    #  sp|Q4PEV8|EIF3M_USTMA
                    accession = accession.split('|')[1]
                
                # save it, unless the gene product name has already changed from the default
                if annot.product_name == DEFAULT_PRODUCT_NAME:
                    annot.product_name = cols[15]

                # if no EC numbers have been set, they can inherit from this
                if len(annot.ec_numbers) == 0:
                    for ec_annot in get_uspdb_ec_nums( accession, cursor ):
                        annot.add_ec_number(ec_annot)

                # if no GO IDs have been set, they can inherit from this
                if len(annot.go_annotations) == 0:
                    for go_annot in get_uspdb_go_terms( accession, cursor ):
                        annot.add_go_annotation(go_annot)

                # remember the ID we just saw
                last_qry_id = this_qry_id


def parse_hmm_evidence( polypeptides, htab_list, cursor ):
    '''
    Reads a list file of HMM evidence and dict of polypeptides, populating each with
    Annotation evidence where appropriate.  Each file in the list can have results
    for multiple queries, but it's assumed that ALL candidate matches for any given
    query are grouped together.

    Currently only the top hit for any given query polypeptide is used.
    '''
    for file in biocodeutils.read_list_file(htab_list):
        last_qry_id = None
        
        for line in open(file):
            line = line.rstrip()
            cols = line.split("\t")
            
            ## only consider the row if the total score is above the total trusted cutoff
            if cols[12] >= cols[17]:
                continue

            this_qry_id = cols[5]
            accession = cols[0]
            version = None

            # if this is a PFAM accession, handle the version
            m = re.match("^(PF\d+)\.\d+", accession)
            if m:
                version = accession
                accession = m.group(1)

            ## the HMM hits are sorted already with the top hit for each query first
            if last_qry_id != this_qry_id:
                ## save it
                annot = polypeptides[this_qry_id].annotation
                annot.product_name = cols[15]
                
                # does our hmm database provide GO terms for this accession?
                for go_annot in get_hmmdb_go_terms( accession, cursor ):
                    annot.add_go_annotation(go_annot)

                ## remember the ID we just saw
                last_qry_id = this_qry_id


def get_uspdb_ec_nums( acc, c ):
    """
    This returns a lsit of bioannotation:ECAnnotation objects
    """
    qry = """
          SELECT us_ec.ec_num
            FROM uniprot_sprot_ec us_ec
                 JOIN uniprot_sprot_acc us_acc ON us_ec.id=us_acc.id
           WHERE us_acc.accession = ?
          """
    c.execute(qry, (acc,))

    ec_annots = list()

    for row in c:
        ec = bioannotation.ECAnnotation(number=row[0])
        ec_annots.append(ec)

    return ec_annots


def get_uspdb_go_terms( acc, c ):
    """
    This returns a list of bioannotation:GOAnnotation objects
    """
    qry = """
          SELECT us_go.go_id
            FROM uniprot_sprot_go us_go
                 JOIN uniprot_sprot_acc us_acc ON us_go.id=us_acc.id
           WHERE us_acc.accession = ?
          """
    c.execute(qry, (acc,))

    go_annots = list()
    
    for row in c:
        go = bioannotation.GOAnnotation(go_id=row[0], ev_code='ISA', with_from=acc)
        go_annots.append(go)
    
    return go_annots

    


def get_hmmdb_go_terms( acc, c ):
    """
    This returns a list of bioannotation:GOAnnotation objects
    """
    qry = "SELECT hg.go_id FROM hmm_go hg JOIN hmm ON hg.hmm_id=hmm.id WHERE hmm.accession = ?"
    c.execute(qry, (acc,))

    go_annots = list()
    
    for row in c:
        go = bioannotation.GOAnnotation(go_id=row[0], ev_code='ISM', with_from=acc)
        go_annots.append(go)
    
    return go_annots



if __name__ == '__main__':
    main()







