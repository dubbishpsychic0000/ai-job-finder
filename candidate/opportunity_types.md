# Opportunity Types — Classification Rules

Every opportunity found must be classified into one of 9 types. The classification is determined by keywords in the listing title and description.

## Type Definitions

### 1. JOB
Standard full-time or part-time employment position.

**Keywords:**
- "emploi"
- "poste"
- "recrutement"
- " CDI"
- "CDD"
- "contrat de travail"
- "offre d'emploi"
- "emplois"

### 2. INTERNSHIP
Temporary position for students/recent graduates to gain practical experience.

**Keywords:**
- "stage"
- "intern"
- "alternance"
- "stagiaire"
- "internship"
- "practicum"

### 3. APPRENTICESHIP
Formal apprenticeship program combining study and work.

**Keywords:**
- "apprentissage"
- "apprentice"
- "formation en alternance"
- "contrat d'apprentissage"

### 4. TRAINING
Educational or professional development program.

**Keywords:**
- "formation"
- "training"
- "cours"
- "programme de formation"
- "certification"

### 5. RECRUITMENT_EVENT
Job fairs, recruitment drives, hiring events.

**Keywords:**
- "salon de l'emploi"
- "job fair"
- "recruitment event"
- "journées de recrutement"
- "forum professionnel"

### 6. RECRUITER_OPPORTUNITY
Opportunity posted by a recruitment agency or headhunter.

**Keywords:**
- "agence de recrutement"
- "recruiter"
- "headhunter"
- " cabinet de recrutement"
- "intérim"

### 7. IMMIGRATION_OPPORTUNITY
Positions that may facilitate immigration (work permit sponsorship, immigration programs).

**Keywords:**
- "visa"
- "work permit"
- "immigration"
- "sponsorship"
- "titre de séjour"
- "permis de travail"

### 8. SCHOLARSHIP
Financial aid or funding for education/study.

**Keywords:**
- "bourse"
- "scholarship"
- "financement études"
- "aide financière"

### 9. GRADUATE_PROGRAM
Structured entry-level programs for recent graduates.

**Keywords:**
- "graduate program"
- "programme de recrutement"
- "jeune diplômé"
- "programme d'orientation"
- "leadership program"

## Classification Priority

The system checks keywords in this order — first match wins:

1. INTERNSHIP (if "stage"/"intern" present)
2. APPRENTICESHIP (if "apprentissage"/"apprentice" present)
3. TRAINING (if "formation"/"training" present)
4. SCHOLARSHIP (if "bourse"/"scholarship" present)
5. IMMIGRATION_OPPORTUNITY (if "visa"/"immigration" present)
6. RECRUITMENT_EVENT (if "salon"/"forum" present)
7. GRADUATE_PROGRAM (if "graduate program"/"jeune diplômé" present)
8. RECRRUITER_OPPORTUNITY (if "agence"/"recruiter" present)
9. JOB (default — anything else)

## Example Classifications

| Listing Text | Classified As | Reason |
|---|---|---|
| "Stage Technicien Bureau d'Études VRD" | INTERNSHIP | Contains "stage" |
| "Offre d'emploi — Technicien Génie Civil" | JOB | No special keywords, default |
| "Bourse d'études en génie civil" | SCHOLARSHIP | Contains "bourse" |
| "Programme de recrutement jeunes diplômés" | GRADUATE_PROGRAM | Contains "jeunes diplômés" + "recrutement" |
| "Salon de l'emploi de Casablanca" | RECRUITMENT_EVENT | Contains "salon de l'emploi" |
