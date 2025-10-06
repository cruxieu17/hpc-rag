
import sys
import os
import torch
import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer
from transformers import AutoTokenizer, AutoModelForSequenceClassification
from transformers import AutoModelForCausalLM
from baseline.prompts import OLD_PROMPT_V2


# Configuration
CHROMA_DIR = os.getenv("CHROMA_PERSIST_DIRECTORY", "./chroma_db")
CHROMA_PERSIST_DIRECTORY: str = os.getenv("CHROMA_PERSIST_DIRECTORY", "./chroma_db")
CHROMA_COLLECTION_NAME: str = os.getenv("CHROMA_COLLECTION_NAME", "text_embeddings")
DEVICE = "cuda:0"
TOP_K = 5


def test_rag(question: str):
    """Run complete RAG pipeline for a question"""
    
    print(f"\n💡 Question: {question}\n")
    
    # 1. RETRIEVAL
    print("🔍 Retrieving from ChromaDB...")
    
    client = chromadb.PersistentClient(path=CHROMA_DIR, settings=Settings(anonymized_telemetry=False))
    collection = client.get_collection(name=CHROMA_COLLECTION_NAME)
    
    embed_model = SentenceTransformer("mixedbread-ai/mxbai-embed-large-v1", device=DEVICE)
    query_vec = embed_model.encode([question], convert_to_tensor=False)[0].tolist()
    
    results = collection.query(
        query_embeddings=[query_vec],
        n_results=10,
        include=["documents", "metadatas", "distances"]
    )
    
    print(f"✓ Retrieved {len(results['documents'][0])} chunks")
    
    # 2. RERANKING
    print("🔄 Reranking...")
    
    rerank_tokenizer = AutoTokenizer.from_pretrained("mixedbread-ai/mxbai-rerank-large-v1")
    rerank_model = AutoModelForSequenceClassification.from_pretrained(
        "mixedbread-ai/mxbai-rerank-large-v1"
    ).to(DEVICE)
    rerank_model.eval()
    
    docs = results['documents'][0]
    inputs = rerank_tokenizer(
        [f"{question} [SEP] {doc}" for doc in docs],
        padding=True,
        truncation=True,
        return_tensors="pt"
    ).to(DEVICE)
    
    with torch.no_grad():
        scores = torch.sigmoid(rerank_model(**inputs).logits).squeeze(-1).cpu().tolist()
    
    # Sort by score and take top K
    ranked = sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)[:TOP_K]
    top_contexts = [doc for doc, _ in ranked]
    
    print(f"  ✓ Selected top {TOP_K} chunks")
    
    # 3. GENERATION
    print("✨ Generating answer...")
    
    tokenizer = AutoTokenizer.from_pretrained("meta-llama/Llama-3.1-8B-Instruct")
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    
    model = AutoModelForCausalLM.from_pretrained(
        "meta-llama/Llama-3.1-8B-Instruct",
        torch_dtype=torch.float16 if DEVICE == "cuda" else torch.float32,
        device_map="auto" if DEVICE == "cuda" else None,
        low_cpu_mem_usage=True
    )
    if DEVICE == "cpu":
        model = model.to("cpu")
    model.eval()
    
    # Create prompt
    prompt = OLD_PROMPT_V2 + "Contexts:\n"
    prompt += "\n".join([f"{i+1}. {c}" for i, c in enumerate(top_contexts)])
    prompt += f"\n\nQuestion: {question}"
    
    # Generate
    inputs = tokenizer(prompt, return_tensors="pt", truncation=True, max_length=4096, padding=True)
    inputs = {k: v.to(model.device) for k, v in inputs.items()}
    
    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_new_tokens=512,
            temperature=0.7,
            do_sample=True,
            top_p=0.9,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id
        )
    
    answer = tokenizer.decode(outputs[0], skip_special_tokens=True)
    if answer.startswith(prompt):
        answer = answer[len(prompt):].strip()
    
    print(f"  ✓ Answer generated\n")
    
    # DISPLAY RESULT
    print("=" * 70)
    print("📝 ANSWER:")
    print("=" * 70)
    print(f"\n{answer}\n")
    print("=" * 70)
    
    return answer


if __name__ == "__main__":
    question = input("\nEnter your question: ").strip()
    
    if not question:
        print("❌ No question provided!")
        sys.exit(1)
    try:
        test_rag(question)
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

