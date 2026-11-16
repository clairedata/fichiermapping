"""tests/test_aligner.py.

Tests unitaires pour le module d'alignement structurel et de coalesce (aligner.py).
"""

from __future__ import annotations

import polars as pl
from src.aligner import align_dataframe
from src.models import MappingAction


def test_align_target_to_source_direct():
    """Vérifie que l'alignement direct target_to_source alimente parfaitement nom avec name sans écrasement."""
    target_columns = ["id_client", "nom", "prenom", "email"]
    df = pl.DataFrame(
        {
            "id": ["10", "20"],
            "name": ["Jade Fournier", "Antoine Leroy"],
            "email": ["jade@test.fr", "antoine@test.fr"],
        }
    )

    # nom prend name, prenom est None
    aligned = align_dataframe(
        df=df,
        target_columns=target_columns,
        target_to_source={
            "id_client": "id",
            "nom": "name",
            "prenom": None,
            "email": "email",
        },
    )

    assert aligned.columns == target_columns
    assert aligned["id_client"].to_list() == ["10", "20"]
    assert aligned["nom"].to_list() == ["Jade Fournier", "Antoine Leroy"]
    assert aligned["prenom"].to_list() == [None, None]
    assert aligned["email"].to_list() == ["jade@test.fr", "antoine@test.fr"]


def test_align_simple_and_order():
    """Vérifie la projection stricte dans l'ordre du schéma cible et l'ajout des colonnes manquantes."""
    target_columns = ["id", "nom", "prenom", "email", "pays"]
    df = pl.DataFrame(
        {
            "prenom": ["Alice", "Bob"],
            "nom": ["Dupont", "Martin"],
            "id": ["1", "2"],
        }
    )

    aligned = align_dataframe(
        df=df,
        target_columns=target_columns,
        matched_columns=["prenom", "nom", "id"],
        mapping_actions={},
    )

    # Ordre strict des colonnes cibles
    assert aligned.columns == target_columns
    assert aligned["id"].to_list() == ["1", "2"]
    assert aligned["nom"].to_list() == ["Dupont", "Martin"]
    assert aligned["prenom"].to_list() == ["Alice", "Bob"]
    # Colonnes cibles manquantes remplies de None
    assert aligned["email"].to_list() == [None, None]
    assert aligned["pays"].to_list() == [None, None]


def test_align_ignored_column():
    """Vérifie que les colonnes marquées comme ignorées sont bien exclues."""
    target_columns = ["id", "nom"]
    df = pl.DataFrame(
        {
            "id": ["1"],
            "nom": ["Durand"],
            "commentaires_internes": ["confidentiel"],
        }
    )

    actions = {
        "commentaires_internes": MappingAction(
            source_col="commentaires_internes",
            target_col=None,
            is_ignored=True,
        )
    }

    aligned = align_dataframe(
        df=df,
        target_columns=target_columns,
        matched_columns=["id", "nom"],
        mapping_actions=actions,
    )

    assert aligned.columns == target_columns
    assert "commentaires_internes" not in aligned.columns


def test_align_coalesce_collision_preserves_non_null():
    """Vérifie la fusion par coalesce en cas de collision.

    Ne doit jamais écraser les données non nulles de la colonne cible existante,
    tout en complétant les valeurs manquantes/vides avec la colonne mappée.
    """
    target_columns = ["id", "telephone"]
    df = pl.DataFrame(
        {
            "id": ["1", "2", "3", "4"],
            # telephone existe déjà dans l'entrant
            "telephone": ["0140000000", None, "", None],
            # tel_secondaire est mappé vers telephone
            "tel_secondaire": ["0699999999", "0612345678", "0788888888", None],
        }
    )

    actions = {
        "tel_secondaire": MappingAction(
            source_col="tel_secondaire",
            target_col="telephone",
            is_ignored=False,
        )
    }

    aligned = align_dataframe(
        df=df,
        target_columns=target_columns,
        matched_columns=["id", "telephone"],
        mapping_actions=actions,
    )

    assert aligned.columns == target_columns
    result_tel = aligned["telephone"].to_list()

    # Ligne 1 : "0140000000" existait déjà -> NE DOIT PAS être écrasé par "0699999999"
    assert result_tel[0] == "0140000000"
    # Ligne 2 : None dans telephone -> complété par "0612345678"
    assert result_tel[1] == "0612345678"
    # Ligne 3 : Chaîne vide "" dans telephone -> complété par "0788888888"
    assert result_tel[2] == "0788888888"
    # Ligne 4 : Deux None -> résultat None
    assert result_tel[3] is None


def test_align_multiple_mapped_sources_coalesce():
    """Vérifie le coalesce quand plusieurs colonnes inconnues sont mappées vers la même cible."""
    target_columns = ["id", "adresse"]
    df = pl.DataFrame(
        {
            "id": ["1", "2", "3"],
            "addr_facturation": [None, "10 Rue de Paris", None],
            "addr_livraison": ["20 Avenue Victor Hugo", "99 Rue Test", None],
        }
    )

    actions = {
        "addr_facturation": MappingAction(
            source_col="addr_facturation",
            target_col="adresse",
        ),
        "addr_livraison": MappingAction(
            source_col="addr_livraison",
            target_col="adresse",
        ),
    }

    aligned = align_dataframe(
        df=df,
        target_columns=target_columns,
        matched_columns=["id"],
        mapping_actions=actions,
    )

    assert aligned["adresse"].to_list() == [
        "20 Avenue Victor Hugo",
        "10 Rue de Paris",
        None,
    ]
