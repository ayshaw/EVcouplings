"""
Uniprot to PDB structure identification and
index mapping using the SIFTS database
(https://www.ebi.ac.uk/pdbe/docs/sifts/)

This functionality is centered around the
pdb_chain_uniprot.csv table available from SIFTS.
(ftp://ftp.ebi.ac.uk/pub/databases/msd/sifts/flatfiles/csv/pdb_chain_uniprot.csv.gz)

Authors:
  Thomas A. Hopf
"""

import pandas as pd
import requests

from evcouplings.align.alignment import (
    Alignment, read_fasta, parse_header
)

from evcouplings.align.protocol import jackhmmer_search
from evcouplings.align.tools import read_hmmer_domtbl
from evcouplings.compare.mapping import alignment_index_mapping, map_indices
from evcouplings.utils.system import (
    get_urllib, ResourceError, valid_file
)
from evcouplings.utils.config import parse_config
from evcouplings.utils.helpers import range_overlap

UNIPROT_MAPPING_URL = "http://www.uniprot.org/mapping/"
SIFTS_URL = "ftp://ftp.ebi.ac.uk/pub/databases/msd/sifts/flatfiles/csv/pdb_chain_uniprot.csv.gz"

JACKHMMER_CONFIG = """
prefix:
sequence_id:
sequence_file:
region:
first_index: 1

use_bitscores: True
domain_threshold: 0.5
sequence_threshold: 0.5
iterations: 1
database: sequence_database

extract_annotation: False
cpu: 1
nobias: False
reuse_alignment: False
checkpoints_hmm: False
checkpoints_ali: False

# database
jackhmmer: ../../software/hmmer-3.1b2-macosx-intel/binaries/jackhmmer
sequence_database:
sequence_download_url: http://www.uniprot.org/uniprot/{}.fasta
"""


def fetch_uniprot_mapping(ids, from_="ACC", to="ACC", format="fasta"):
    """
    Fetch data from UniProt ID mapping service
    (e.g. download set of sequences)

    Parameters
    ----------
    ids : list(str)
        List of UniProt identifiers for which to
        retrieve mapping
    from_ : str, optional (default: "ACC")
        Source identifier (i.e. contained in "ids" list)
    to : str, optional (default: "ACC")
        Target identifier (to which source should be mapped)
    format : str, optional (default: "fasta")
        Output format to request from Uniprot server

    Returns
    -------
    str:
        Response from UniProt server
    """
    params = {
        "from": from_,
        "to": to,
        "format": format,
        "query": " ".join(ids)
    }
    url = UNIPROT_MAPPING_URL
    r = requests.post(url, data=params)

    if r.status_code != requests.codes.ok:
        raise ResourceError(
            "Invalid status code ({}) for URL: {}".format(
                r.status_code, url
            )
        )

    return r.text


def find_homologs_jackhmmer(**kwargs):
    """
    Identify homologs using jackhmmer

    Parameters
    ----------
    **kwargs
        Passed into jackhmmer_search protocol
        (see documentation for available options)

    Returns
    -------
    ali : evcouplings.align.Alignment
        Alignment of homologs of query sequence
        in sequence database
    hits : pandas.DataFrame
        Tabular representation of hits
    """
    # load default configuration
    config = parse_config(JACKHMMER_CONFIG)

    # update with overrides from kwargs
    config = {
        **config,
        **kwargs,
    }

    # create temporary output if no prefix is given
    if config["prefix"] is None:
        config["prefix"] = path.join(tempdir(), "compare")

    # run jackhmmer against sequence database
    ar = jackhmmer_search(**config)

    with open(ar["raw_alignment_file"]) as a:
        ali = Alignment.from_file(a, "stockholm")

    # read hmmer hittable and simplify
    hits = read_hmmer_domtbl(ar["hittable_file"])

    hits.loc[:, "uniprot_ac"] = hits.loc[:, "target_name"].map(lambda x: x.split("|")[1])
    hits.loc[:, "uniprot_id"] = hits.loc[:, "target_name"].map(lambda x: x.split("|")[2])

    hits = hits.rename(
        columns={
            "domain_score": "bitscore",
            "domain_i_Evalue": "e_value",
            "ali_from": "alignment_start",
            "ali_to": "alignment_end",
        }
    )

    hits.loc[:, "alignment_start"] = pd.to_numeric(hits.alignment_start).astype(int)
    hits.loc[:, "alignment_end"] = pd.to_numeric(hits.alignment_end).astype(int)

    hits.loc[:, "alignment_id"] = (
        hits.target_name + "/" +
        hits.alignment_start.astype(str) + "-" +
        hits.alignment_end.astype(str)
    )

    hits = hits.loc[
        :, ["alignment_id", "uniprot_ac", "uniprot_id", "alignment_start",
            "alignment_end", "bitscore", "e_value"]
    ]

    return ali, hits


class SIFTSResult:
    """
    Store results of SIFTS structure/mapping identification.

    (Full class defined for easify modification of fields)
    """
    def __init__(self, hits, mapping):
        """
        Create new SIFTS structure / mapping record.

        Parameters
        ----------
        hits : pandas.DataFrame
            Table with identified PDB chains
        mapping : dict
            Mapping from seqres to Uniprot numbering
            for each PDB chain
            (index by mapping_index column in hits
            dataframe)
        """
        self.hits = hits
        self.mapping = mapping


class SIFTS:
    """
    Provide Uniprot to PDB mapping data and functions
    starting from SIFTS mapping table.
    """
    def __init__(self, sifts_table_file, sequence_file=None):
        """
        Create new SIFTS mapper from mapping table.

        Parameters
        ----------
        sifts_table_file : str
            Path to SIFTS pdb_chain_uniprot.csv
        sequence_file : str, optional (default: None)
            Path to file containing all UniProt sequences
            in SIFTS (used for homology-based identification
            of structures).
            Note: This file can be created using the
            create_sequence_file() method.
        """
        # test if table exists, if not, download
        if not valid_file(sifts_table_file):
            get_urllib(SIFTS_URL, sifts_table_file)

        # load table and rename columns for internal use, if SIFTS
        # ever decided to rename theirs
        self.table = pd.read_csv(
            sifts_table_file, comment="#"
        ).rename(
            columns={
                "PDB": "pdb_id",
                "CHAIN": "pdb_chain",
                "SP_PRIMARY": "uniprot_ac",
                "RES_BEG": "resseq_start",
                "RES_END": "resseq_end",
                "PDB_BEG": "coord_start",
                "PDB_END": "coord_end",
                "SP_BEG": "uniprot_start",
                "SP_END": "uniprot_end",
            }
        )

        # Problem: for a subset of entries, the seqres and Uniprot
        # ranges do not align (different lengths). For now, we have
        # to drop these, the only solution would be to use the
        # individual SIFTS xml files.
        self.table = self.table.query(
            "(resseq_end - resseq_start) == (uniprot_end - uniprot_start)"
        )

        self.sequence_file = sequence_file

        # if path for sequence file given, but not there, create
        if sequence_file is not None and not valid_file(sequence_file):
            self.create_sequence_file(sequence_file)

        # add Uniprot ID column if we have sequence mapping
        # from FASTA file
        if self.sequence_file is not None:
            self._add_uniprot_ids()

    def _add_uniprot_ids(self):
        """
        Add Uniprot ID column to SIFTS table based on
        AC to ID mapping extracted from sequence database
        """
        # iterate through headers in sequence file and store
        # AC to ID mapping
        ac_to_id = {}
        with open(self.sequence_file) as f:
            for seq_id, _ in read_fasta(f):
                _, ac, id_ = seq_id.split(" ")[0].split("|")
                ac_to_id[ac] = id_

        # add column to dataframe
        self.table.loc[:, "uniprot_id"] = self.table.loc[:, "uniprot_ac"].map(ac_to_id)

    def create_sequence_file(self, output_file):
        """
        Create FASTA sequence file containing all UniProt
        sequences of proteins in SIFTS. This file is required
        for homology-based structure identification and
        index remapping.
        This function will also automatically associate
        the sequence file with the SIFTS object.

        Parameters
        ----------
        output_file : str
            Path at which to store sequence file
        """
        ids = self.table.uniprot_ac.unique().tolist()

        CHUNK_SIZE = 1000
        chunks = [
            ids[i:i + CHUNK_SIZE] for i in range(0, len(ids), CHUNK_SIZE)
        ]

        with open(output_file, "w") as f:
            for ch in chunks:
                # fetch sequence chunk
                seqs = fetch_uniprot_mapping(ch)

                # then store to FASTA file
                f.write(seqs)

        self.sequence_file = output_file

        # add Uniprot ID column to SIFTS table
        self._add_uniprot_ids()

    def _create_sequence_file(self, output_file):
        """
        Create FASTA sequence file containing all UniProt
        sequences of proteins in SIFTS. This file is required
        for homology-based structure identification and
        index remapping.
        This function will also automatically associate
        the sequence file with the SIFTS object.

        Note: this would be the nicer function, but unfortunately
        the UniProt server frequently closes the connection running it

        Parameters
        ----------
        output_file : str
            Path at which to store sequence file
        """
        # fetch all the sequences
        seqs = fetch_uniprot_mapping(
            self.table.uniprot_ac.unique().tolist()
        )

        # then store to FASTA file
        with open(output_file, "w") as f:
            f.write(seqs)

        self.sequence_file = output_file

    def _finalize_hits(self, hit_segments):
        """
        Create final hit/mapping record from
        table of segments in PDB chains in
        SIFTS file.

        Parameters
        ----------
        hit_segments : pd.DataFrame
            Subset of self.table that will be
            turned into final mapping record

        Returns
        -------
        SIFTSResult
            Identified hits plus index mappings
            to Uniprot
        """
        # compile final set of hits
        hits = []

        # compile mapping from Uniprot to seqres for
        # each final hit
        mappings = {}

        # go through all SIFTS segments per PDB chain
        for i, ((pdb_id, pdb_chain), chain_grp) in enumerate(
            hit_segments.groupby(["pdb_id", "pdb_chain"])
        ):
            # put segments together in one segment-based
            # mapping for chain; this will be used by pdb.Chain.remap()
            mapping = {
                (r["resseq_start"], r["resseq_end"]): (r["uniprot_start"], r["uniprot_end"])
                for j, r in chain_grp.iterrows()
            }

            # append current hit and mapping
            hits.append([pdb_id, pdb_chain, i])
            mappings[i] = mapping

        # create final hit representation as DataFrame
        hits_df = pd.DataFrame(
            hits, columns=["pdb_id", "pdb_chain", "mapping_index"]
        )

        return SIFTSResult(hits_df, mappings)

    def by_pdb_id(self, pdb_id, pdb_chain=None, uniprot_ac=None):
        """
        Find structures and mapping by PDB id
        and chain name

        Parameters
        ----------
        pdb_id : str
            4-letter PDB identifier
        pdb_chain : str, optional (default: None)
            PDB chain name (if not given, all
            chains for PDB entry will be returned)
        uniprot_ac : str, optional (default: None)
            Filter to keep only this Uniprot accession
            number (necessary for chimeras, or multi-
            chain complexes with differen proteins)

        Returns
        -------
        SIFTSResult
            Identified hits plus index mappings
            to Uniprot

        Raises
        ------
        ValueError
            If selected segments in PDB file do
            not unambigously map to one Uniprot
            entry
        """
        pdb_id = pdb_id.lower()
        query = "pdb_id == @pdb_id"

        # filter by PDB chain if selected
        if pdb_chain is not None:
            query += " and pdb_chain == @pdb_chain"

        # filter by UniProt AC if selected
        # (to remove chimeras)
        if uniprot_ac is not None:
            query += " and uniprot_ac == @uniprot_ac"

        x = self.table.query(query)

        # check we only have one protein (might not
        # be the case with multiple chains, or with
        # chimeras)
        if len(x.uniprot_ac.unique()) > 1:
            raise ValueError(
                "Multiple Uniprot sequences on chains, "
                "please disambiguate using uniprot_ac "
                "parameter: {}".format(
                    ", ".join(x.uniprot_ac.unique())
                )
            )

        # create hit and mapping result
        return self._finalize_hits(x)

    def by_uniprot_id(self, uniprot_id, reduce_chains=False):
        """
        Find structures and mapping by Uniprot
        access number.

        Parameters
        ----------
        uniprot_ac : str
            Find PDB structures for this Uniprot accession
            number. If sequence_file was given while creating
            the SIFTS object, Uniprot identifiers can also be
            used.
        reduce_chains : bool, optional (Default: True)
            If true, keep only first chain per PDB ID
            (i.e. remove redundant occurrences of same
            protein in PDB structures). Should be set to
            False to identify homomultimeric contacts.

        Returns
        -------
        SIFTSResult
            Record of hits and mappings found for this
            Uniprot protein. See by_pdb_id() for detailed
            explanation of fields.
        """
        query = "uniprot_ac == @uniprot_id"

        if "uniprot_id" in self.table.columns:
            query += " or uniprot_id == @uniprot_id"

        x = self.table.query(query)

        hit_table = self._finalize_hits(x)

        # only retain one chain if this option is active
        if reduce_chains:
            hit_table.hits = hit_table.hits.groupby(
                "pdb_id"
            ).first().reset_index()

        return hit_table

    def by_alignment(self, min_overlap=20, reduce_chains=False, **kwargs):
        """
        Find structures by sequence alignment between
        query sequence and sequences in PDB.

        # TODO: offer option to start from HMM profile for this

        Parameters
        ----------
        min_overlap : int, optional (default: 20)
            Require at least this many aligned positions
            with the target structure
        reduce_chains : bool, optional (Default: True)
            If true, keep only first chain per PDB ID
            (i.e. remove redundant occurrences of same
            protein in PDB structures). Should be set to
            False to identify homomultimeric contacts.
        **kwargs
            Passed into jackhmmer_search protocol
            (see documentation for available options).
            Additionally, if "prefix" is given, individual
            mappings will be saved to files suffixed by
            the respective key in mapping table.

        Returns
        -------
        SIFTSResult
            Record of hits and mappings found for this
            query sequence by alignment. See by_pdb_id()
            for detailed explanation of fields.
        """
        def _create_mapping(i, r):
            _, query_start, query_end = parse_header(ali.ids[0])

            # create mapping from query into PDB Uniprot sequence
            # A_i will be query sequence indeces, A_j Uniprot sequence indeces
            m = map_indices(
                ali[0], query_start, query_end,
                ali[r["alignment_id"]], r["alignment_start"], r["alignment_end"]
            )

            # create mapping from PDB Uniprot into seqres numbering
            # j will be Uniprot sequence index, k seqres index
            n = pd.DataFrame(
                {
                    "j": list(range(r["uniprot_start"], r["uniprot_end"] + 1)),
                    "k": list(range(r["resseq_start"], r["resseq_end"] + 1)),
                }
            )

            # need to convert to strings since other mapping has indeces as strings
            n.loc[:, "j"] = n.j.astype(str)
            n.loc[:, "k"] = n.k.astype(str)

            # join over Uniprot indeces (i.e. j);
            # get rid of any position that is not aligned
            mn = m.merge(n, on="j", how="inner").dropna()

            # store index mappings if filename prefix is given
            prefix = kwargs.get("prefix", None)
            if prefix is not None:
                mn.to_csv("{}_{}.csv".format(prefix, i), index=False)

            # extract final mapping from query (k) to seqres (i)
            map_ = dict(
                zip(mn.k, mn.i)
            )

            return map_

        if self.sequence_file is None:
            raise ValueError(
                "Need to have SIFTS sequence file. "
                "Create using create_sequence_file() "
                "method or constructor."
            )

        ali, hits = find_homologs_jackhmmer(
            sequence_database=self.sequence_file, **kwargs
        )

        # merge with internal table to identify overlap of
        # aligned regions and regions with structural coverage
        hits = hits.merge(
            self.table, on="uniprot_ac", suffixes=("", "_")
        )

        hits.loc[:, "overlap"] = [
            range_overlap(
                (r["uniprot_start"], r["uniprot_end"]),
                (r["alignment_start"], r["alignment_end"])
            ) for i, r in hits.iterrows()
        ]

        # remove hits with too little residue coverage
        hits = hits.query("overlap >= @min_overlap")

        # if requested, only keep one chain per PDB;
        # sort by score before this to keep best hit
        if reduce_chains:
            hits = hits.sort_values(by="bitscore", ascending=False)
            hits = hits.groupby("pdb_id").first().reset_index()

        # create mappings and store in SIFTSResult object
        mappings = {
            i: _create_mapping(i, r) for i, r in hits.iterrows()
        }

        # put mapping index into column
        hits = hits.reset_index().rename(
            columns={"index": "mapping_index"}
        )

        return SIFTSResult(hits, mappings)
